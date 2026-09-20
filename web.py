"""
Flask web dashboard (Koyeb) — Subject → Topic → Flashcards.

Routes:
  /login            (GET/POST)  TG user id + DASHBOARD_SECRET
  /logout
  /                 subject cards (with per-subject accuracy)
  /subject/<name>   topic cards
  /topic/<name>/<topic>  flashcards: wrong answers, spaced-repetition order,
                          question image + options + explanation (flip)
  /img/<code>       photo proxy (downloads via the bot client)

Auth: session['uid']. Only DASH_ALLOWED_TG_IDS may log in (default: OWNER_ID).
"""

from __future__ import annotations

import asyncio
import io
import logging
from datetime import datetime, timezone

from flask import (
    Flask,
    abort,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)

import config
from database import Database

log = logging.getLogger("web")

app_flask = Flask(__name__, template_folder="templates")
app_flask.secret_key = config.DASHBOARD_SECRET or "dev-insecure-secret-change-me"

# Set from bot.py: the running pyrogram client + its event loop (for downloads).
_bot_app = None
_bot_loop: asyncio.AbstractEventLoop | None = None

CARDS_PER_PAGE = 30


def start_web(db: Database, in_thread: bool = True, port: int | None = None) -> Flask:
    """Bind the DB + bot client and (optionally) serve in a background thread."""
    app_flask.config["DB"] = db

    if not in_thread:
        _serve(port or config.WEB_PORT)
        return app_flask

    import threading

    t = threading.Thread(target=_serve, args=(port or config.WEB_PORT,), daemon=True)
    t.start()
    log.info("Flask dashboard thread started on :%s", port or config.WEB_PORT)
    return app_flask


def _serve(port: int) -> None:
    # threaded=True so a slow /img download can't block the health check.
    app_flask.run(host="0.0.0.0", port=port, debug=False, threaded=True)


def set_bot(app, loop: asyncio.AbstractEventLoop) -> None:
    global _bot_app, _bot_loop
    _bot_app, _bot_loop = app, loop


def db() -> Database:
    return app_flask.config["DB"]


def login_required(fn):
    from functools import wraps

    @wraps(fn)
    def _wrap(*a, **kw):
        uid = session.get("uid")
        if not uid:
            return redirect(url_for("login"))
        return fn(*a, **kw)

    return _wrap


def _fmt_pct(x: float) -> str:
    return f"{x:.0f}%"


# ─── auth ──────────────────────────────────────────────────────────────────────
@app_flask.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        uid_raw = (request.form.get("uid") or "").strip()
        secret = (request.form.get("secret") or "").strip()
        try:
            uid = int(uid_raw)
        except ValueError:
            error = "User ID numeric hona chahiye"
        else:
            allowed = config.DASH_ALLOWED_TG_IDS
            if not allowed:
                error = "Web dashboard disabled — DASH_ALLOWED_TG_IDS empty"
            elif uid not in allowed:
                error = "Yeh user id allowed nahi hai"
            elif secret != config.DASHBOARD_SECRET:
                error = "Galat secret key"
            else:
                session["uid"] = uid
                return redirect(url_for("index"))
    return render_template("login.html", error=error)


@app_flask.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ─── health check (Koyeb) ──────────────────────────────────────────────────────
@app_flask.route("/health")
def health():
    return {"ok": True, "db": db().enabled}


# ─── index: subjects ──────────────────────────────────────────────────────────
@app_flask.route("/")
@login_required
def index():
    uid = session["uid"]
    subjects = sorted(db().col("questions").distinct("subject"))
    rows = []
    for s in subjects:
        if not s:
            continue
        total = db().col("questions").count_documents({"subject": s})
        topics = db().col("questions").distinct("topic", {"subject": s})
        att = db().col("sessions").count_documents(
            {"user_id": uid, "subject": s, "is_correct": False}
        )
        rows.append(
            {
                "name": s,
                "total": total,
                "topics": len([t for t in topics if t]),
                "wrong": att,
            }
        )
    stats = sync(db().user_stats(uid))
    return render_template("subjects.html", subjects=rows, stats=stats, uid=uid)


# ─── subject → topics ─────────────────────────────────────────────────────────
@app_flask.route("/subject/<subject>")
@login_required
def subject(subject: str):
    uid = session["uid"]
    subject = subject.upper()
    topics = sorted(db().col("questions").distinct("topic", {"subject": subject}))
    rows = []
    for t in topics:
        if not t:
            continue
        total = db().col("questions").count_documents({"subject": subject, "topic": t})
        wrong = db().col("sessions").count_documents(
            {"user_id": uid, "subject": subject, "topic": t, "is_correct": False}
        )
        rows.append({"name": t, "total": total, "wrong": wrong})
    return render_template(
        "topics.html", subject=subject, topics=rows, uid=uid
    )


# ─── topic → flashcards ───────────────────────────────────────────────────────
@app_flask.route("/topic/<subject>/<topic>")
@login_required
def topic(subject: str, topic: str):
    uid = session["uid"]
    subject, topic = subject.upper(), topic

    # 1) wrong attempts in this subject+topic (newest first), then join questions
    cur = (
        db().col("sessions")
        .find(
            {
                "user_id": uid,
                "subject": subject,
                "topic": topic,
                "is_correct": False,
            }
        )
        .sort("answered_at", -1)
        .limit(CARDS_PER_PAGE)
    )
    sessions = cur.to_list(length=CARDS_PER_PAGE) if hasattr(cur, "to_list") else list(cur)
    codes = [s["question_code"] for s in sessions if s.get("question_code")]
    questions = {}
    if codes:
        for q in db().col("questions").find({"_id": {"$in": codes}}):
            questions[q["_id"]] = q

    # 2) SRS due state per code (ordering: most overdue first)
    srs = {}
    if codes:
        for s in db().col("srs").find(
            {"user_id": uid, "question_code": {"$in": codes}}
        ):
            srs[s["question_code"]] = s

    cards = []
    seen = set()
    for s in sessions:
        code = s.get("question_code")
        if not code or code in seen:
            continue
        seen.add(code)
        q = questions.get(code, {})
        srs_state = srs.get(code, {})
        cards.append(
            {
                "code": code,
                "options": q.get("options") or [],
                "correct_idx": q.get("correct_idx", -1),
                "chosen_idx": s.get("chosen_idx", -1),
                "explanation": (q.get("explanation") or "—").strip(),
                "has_image": bool(q.get("file_id")),
                "due": srs_state.get("due_at"),
                "ease": srs_state.get("ease", 2.5),
                "interval": srs_state.get("interval", 0),
            }
        )

    # overdue/least-ease first (spaced repetition)
    now = datetime.now(timezone.utc)
    cards.sort(key=lambda c: (c["due"] or now, c["ease"]))

    return render_template(
        "flashcards.html",
        subject=subject,
        topic=topic,
        cards=cards,
        uid=uid,
    )


# ─── photo proxy ──────────────────────────────────────────────────────────────
@app_flask.route("/img/<code>")
@login_required
def img(code: str):
    q = db().get_question(code.upper())
    if not q or not q.get("file_id"):
        abort(404)
    if _bot_app is None or _bot_loop is None:
        abort(503, "bot client not ready")
    try:
        fut = asyncio.run_coroutine_threadsafe(
            _download(q["file_id"]), _bot_loop
        )
        data = fut.result(timeout=60)
    except Exception as e:  # noqa: BLE001
        log.error("img download failed for %s: %s", code, e)
        abort(500)
    if data is None:
        abort(404)
    return send_file(io.BytesIO(data), mimetype="image/jpeg")


async def _download(file_id: str) -> bytes | None:
    """Download photo bytes via the pyrogram client (runs on bot loop)."""
    buf = await _bot_app.download_media(file_id, in_memory=True)
    if buf is None:
        return None
    return buf.getvalue() if hasattr(buf, "getvalue") else bytes(buf)


def sync(coro):
    """Run an async DB call from the sync Flask thread on the bot loop."""
    if _bot_loop is None:
        # Fallback: no bot loop yet (rare) — run in a throwaway loop
        return asyncio.run(coro)
    fut = asyncio.run_coroutine_threadsafe(coro, _bot_loop)
    return fut.result(timeout=30)
