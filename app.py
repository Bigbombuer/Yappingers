import os, re, json, time
import requests
from flask import Flask, request, jsonify, render_template
from playwright.sync_api import sync_playwright

# ======================================================
# CONFIG (GitHub Deploy)
# ======================================================
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
X_AUTH_TOKEN = os.getenv("X_AUTH_TOKEN")
X_CT0 = os.getenv("X_CT0")

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
MODEL = os.getenv("GROQ_MODEL", "llama-3.1-8b-instant")

X_PROFILE_DIR = os.getenv("X_PROFILE_DIR", "x_profile")

MAX_TWEETS = int(os.getenv("MAX_TWEETS", "35"))
MAX_SCAN = int(os.getenv("MAX_SCAN", "120"))  # berapa tweet discan sebelum stop (buat keyword filter)

app = Flask(__name__)

# ======================================================
# UTILS
# ======================================================
def clean_text(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()

def format_thread(lst):
    lst = [x.strip() for x in (lst or []) if str(x).strip()]
    n = len(lst)
    return "\n\n".join([f"({i}/{n}) {t}" for i, t in enumerate(lst, 1)])

def parse_keywords(s: str):
    """
    "airdrop, points, testnet" -> ["airdrop","points","testnet"]
    """
    if not s:
        return []
    parts = [x.strip().lower() for x in s.split(",")]
    return [x for x in parts if x]

def tweet_match_keywords(tweet: str, keywords: list[str]) -> bool:
    """
    Kalau keywords kosong -> always true
    Kalau ada -> true jika tweet mengandung salah satu keyword
    """
    if not keywords:
        return True
    low = (tweet or "").lower()
    return any(k in low for k in keywords)

def require_secrets():
    missing = []
    if not GROQ_API_KEY: missing.append("GROQ_API_KEY")
    if not X_AUTH_TOKEN: missing.append("X_AUTH_TOKEN")
    if not X_CT0: missing.append("X_CT0")
    if missing:
        raise RuntimeError("Secrets belum lengkap: " + ", ".join(missing))

# ======================================================
# GROQ
# ======================================================
def groq_chat(system: str, user: str, temperature=0.85) -> str:
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": MODEL,
        "temperature": temperature,
        "messages": [
            {"role": "system", "content": system.strip()},
            {"role": "user", "content": user.strip()},
        ],
    }
    r = requests.post(GROQ_URL, headers=headers, json=payload, timeout=120)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]

def extract_json(text: str) -> dict:
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("Model tidak mengembalikan JSON valid.")
    return json.loads(text[start:end+1])

# ======================================================
# X SCRAPER (Playwright + Cookie Inject)
# ======================================================
def open_x_context(p):
    os.makedirs(X_PROFILE_DIR, exist_ok=True)
    return p.chromium.launch_persistent_context(
        user_data_dir=X_PROFILE_DIR,
        headless=True,
        viewport={"width": 430, "height": 900},
        locale="en-US",
    )

def inject_x_cookies(ctx):
    ctx.add_cookies([
        {
            "name": "auth_token",
            "value": X_AUTH_TOKEN,
            "domain": ".x.com",
            "path": "/",
            "httpOnly": True,
            "secure": True,
            "sameSite": "Lax",
        },
        {
            "name": "ct0",
            "value": X_CT0,
            "domain": ".x.com",
            "path": "/",
            "httpOnly": False,
            "secure": True,
            "sameSite": "Lax",
        },
    ])

def is_logged_in(page) -> bool:
    page.goto("https://x.com/home", wait_until="domcontentloaded")
    time.sleep(2)

    if "login" in page.url:
        return False

    html = (page.content() or "").lower()
    if "log in" in html and "sign in" in html:
        return False

    return True

def looks_like_spam(t: str) -> bool:
    low = (t or "").lower()
    spam_kw = [
        "gm", "good morning", "gn", "goodnight", "wagmi",
        "giveaway", "retweet", "tag", "follow", "like",
        "winner", "prize"
    ]
    if any(k in low for k in spam_kw):
        return True
    if len(t) < 60:
        return True
    return False

def fetch_user_tweets(username: str, limit=25, keywords=None):
    username = username.replace("@", "").strip()
    limit = max(10, min(int(limit), MAX_TWEETS))
    keywords = keywords or []

    with sync_playwright() as p:
        ctx = open_x_context(p)
        inject_x_cookies(ctx)

        page = ctx.new_page()

        if not is_logged_in(page):
            ctx.close()
            raise RuntimeError("Cookie invalid/expired. Ambil ulang auth_token + ct0.")

        page.goto(f"https://x.com/{username}", wait_until="domcontentloaded")
        time.sleep(3)

        tweets, seen = [], set()
        scanned = 0

        for _ in range(14):
            articles = page.query_selector_all("article")

            for art in articles:
                if scanned >= MAX_SCAN:
                    break

                try:
                    raw = clean_text(art.inner_text())

                    if "reposted" in raw.lower():
                        continue
                    if looks_like_spam(raw):
                        continue
                    if raw in seen:
                        continue

                    seen.add(raw)
                    scanned += 1

                    if not tweet_match_keywords(raw, keywords):
                        continue

                    tweets.append(raw)

                    if len(tweets) >= limit:
                        break
                except:
                    pass

            if scanned >= MAX_SCAN or len(tweets) >= limit:
                break

            page.mouse.wheel(0, 2000)
            time.sleep(1.1)

        ctx.close()

    return tweets[:limit]

# ======================================================
# THREAD GENERATOR
# ======================================================
def gen_thread_from_tweets(username: str, tweets: list[str], style="alpha") -> dict:
    style = (style or "alpha").lower().strip()
    if style not in ["alpha", "savage", "degen"]:
        style = "alpha"

    tone = {
        "alpha": "Santai pro, fokus insight + strategi. Minim lebay.",
        "savage": "Tajam + sedikit nyindir, tapi tetap sopan. Fokus red flags & miskonsepsi.",
        "degen": "Degen lucu, rame, tapi tetap informatif dan actionable."
    }[style]

    joined = "\n\n---\n\n".join(tweets[:40])
    source = f"https://x.com/{username.replace('@','')}"

    system = f"""
Lu adalah penulis thread X crypto Indonesia niche AI + Airdrop.
Style: {tone}

Rules:
- tweet 1 hook harus NYANTOL, jangan "Apa itu..."
- santai, natural, gak kaku
- jangan ngarang detail yang gak ada di tweet
- kalau token/airdrop belum jelas: tulis "belum ada info resmi"
- output JSON valid tanpa markdown
"""

    user = f"""
Sumber: {source}

Tweet terbaru:
\"\"\"
{joined}
\"\"\"

Output JSON:
{{
  "project_name": "...",
  "thread_clean": ["... (10-12 tweets)"],
  "thread_degen": ["... (10-12 tweets)"],
  "hooks": ["...", "...", "..."],
  "cheatcodes": ["...", "...", "...", "..."],
  "red_flags": ["...", "...", "..."],
  "cta": "..."
}}

Constraints:
- Clean & Degen WAJIB 10–12 tweet
- Harus ada: recap update, alpha angle, cara ikut/farming, warning/redflags, cheatcode, CTA
- max 260 karakter per tweet
"""

    raw = groq_chat(system, user, temperature=0.87)
    return extract_json(raw)

# ======================================================
# ROUTES
# ======================================================
@app.get("/")
def home():
    return render_template("index.html")

@app.get("/health")
def health():
    try:
        require_secrets()
        return jsonify({"ok": True, "msg": "Secrets OK"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.post("/api/generate")
def api_generate():
    data = request.get_json(force=True)

    username = (data.get("username") or "").strip()
    style = (data.get("style") or "alpha").strip()
    limit = int(data.get("limit") or 25)

    keywords_raw = (data.get("keywords") or "").strip()
    strict = bool(data.get("strict") or False)

    if not username:
        return jsonify({"ok": False, "error": "Username kosong"}), 400

    try:
        require_secrets()

        keywords = parse_keywords(keywords_raw)

        # 1) coba keyword dulu
        tweets = fetch_user_tweets(username, limit=limit, keywords=keywords)

        # 2) fallback kalau gak strict & hasil sedikit
        if not strict and keywords and len(tweets) < 8:
            tweets = fetch_user_tweets(username, limit=limit, keywords=[])

        if len(tweets) < 8:
            return jsonify({
                "ok": False,
                "error": "Tweet kebaca terlalu sedikit (akun private / jarang tweet / keyword terlalu ketat)."
            }), 400

        pack = gen_thread_from_tweets(username, tweets, style=style)

        return jsonify({
            "ok": True,
            "source": f"https://x.com/{username.replace('@','')}",
            "tweets_used": len(tweets),
            "keywords_used": keywords,
            "strict_mode": strict,
            "hooks": pack.get("hooks", []),
            "cheatcodes": pack.get("cheatcodes", []),
            "red_flags": pack.get("red_flags", []),
            "cta": pack.get("cta", ""),
            "thread_clean_text": format_thread(pack.get("thread_clean", [])),
            "thread_degen_text": format_thread(pack.get("thread_degen", [])),
        })

    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# ======================================================
# ENTRYPOINT
# ======================================================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "3000")))
