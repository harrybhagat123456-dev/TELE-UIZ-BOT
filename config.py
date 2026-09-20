# ════════════════════════════════════════════════════════════════════════════════
# Quiz Practice Bot — Config (Koyeb deploy)
# ════════════════════════════════════════════════════════════════════════════════

import os
from dotenv import load_dotenv

load_dotenv()


def _int(name: str, default: int = 0) -> int:
    try:
        return int(str(os.getenv(name) or "").strip())
    except (TypeError, ValueError):
        return default


def _list(name: str) -> list[int]:
    """Space/comma-separated int list (OWNER_ID etc.)."""
    raw = str(os.getenv(name) or "").replace(",", " ").split()
    out = []
    for part in raw:
        try:
            out.append(int(part))
        except ValueError:
            continue
    return out


# ─── Telegram client (REQUIRED) ──────────────────────────────────────────────────
API_ID = os.getenv("API_ID", "").strip()
API_HASH = os.getenv("API_HASH", "").strip()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

# ─── Database (REQUIRED) ────────────────────────────────────────────────────────
# MONGO_DB is the full URI (kept for parity with the other project).
MONGO_DB = os.getenv("MONGO_DB") or os.getenv("DATABASE_URL") or ""
DB_NAME = os.getenv("DB_NAME", "quiz_practice_bot")

# ─── Identity / control ─────────────────────────────────────────────────────────
OWNER_ID = _list("OWNER_ID")
ADMIN_IDS = set(_list("ADMIN_IDS")) | set(OWNER_ID)

# Channel where the bot will POST practice polls (REQUIRED).
# Accept -100... id, @username, or https://t.me/<username>
PRACTICE_CHAT = os.getenv("PRACTICE_CHAT", "").strip()

# ─── Behaviour ──────────────────────────────────────────────────────────────────
# Telegram hard cap for poll open_period is 600s (10 min). We post a quiz poll,
# it stays open 10 min, then the bot marks it "expired" in DB (votes after the
# window don't count) and edits a reveal message with the answer + explanation.
POLL_OPEN_SECONDS = min(600, max(60, _int("POLL_OPEN_SECONDS", 600)))

# Seconds after posting before the bot stops accepting votes for that poll
# (window the dashboard counts as "attempted").
VOTE_WINDOW_SECONDS = _int("VOTE_WINDOW_SECONDS", 3600)

# Rate-limit: minimum gap between two generated polls (Telegram FloodWait safety).
POLL_SEND_GAP = _int("POLL_SEND_GAP", 3)

# Scanner: how far ahead (in messages) to search for the explanation of a question.
EXPLANATION_LOOKAHEAD = _int("EXPLANATION_LOOKAHEAD", 200)

# Scanner: channel(s) to scrape — space separated @usernames / ids / t.me links.
SOURCE_CHATS = [c for c in os.getenv("SOURCE_CHATS", "").split() if c]

# Web dashboard
WEB_PORT = _int("PORT", _int("WEB_PORT", 8080))
DASHBOARD_SECRET = os.getenv("DASHBOARD_SECRET", "").strip()

# Telegram admin ids allowed to log into the web dashboard (see dashboard/login).
# If empty, web dashboard is disabled for safety.
DASH_ALLOWED_TG_IDS = set(_list("DASH_ALLOWED_TG_IDS")) | set(OWNER_ID)

# ─── Sanity checks (non-fatal; warns only) ──────────────────────────────────────
if not all([API_ID, API_HASH, BOT_TOKEN]):
    print("[CONFIG] WARN: API_ID/API_HASH/BOT_TOKEN missing — bot will not start")

if BOT_TOKEN:
    parts = BOT_TOKEN.split(":", 1)
    if len(parts) != 2 or not parts[0].isdigit():
        print(f"[CONFIG] WARN: BOT_TOKEN looks invalid: '{BOT_TOKEN[:16]}...'")

if not MONGO_DB:
    print("[CONFIG] WARN: MONGO_DB missing — scanner/tracker will not work")

if not PRACTICE_CHAT:
    print("[CONFIG] WARN: PRACTICE_CHAT missing — /gen has nowhere to post polls")
