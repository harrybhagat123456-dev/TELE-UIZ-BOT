"""
Telegram-side dashboard commands (until the web dashboard is built).

/start      — welcome message + list of commands
/help       — usage guide
/stats      — overall accuracy, streak, subject-wise breakdown
/wrong      — latest wrong questions (with options, correct answer, explanation)
/bookmarks  — saved wrong questions
/bm SC12    — toggle bookmark on a question
/leaderboard— top users
/review     — SM-2 spaced-repetition queue (due flashcards)
"""

from __future__ import annotations

import logging

from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

import config
from database import Database

log = logging.getLogger(__name__)

LETTERS = "ABCD"


def fmt_options(options: list[str], correct_idx: int, chosen_idx: int | None) -> str:
    out = []
    for i, opt in enumerate(options or []):
        mark = ""
        if i == correct_idx:
            mark = " ✅"
        if chosen_idx is not None and i == chosen_idx and i != correct_idx:
            mark = " ❌"
        out.append(f"{LETTERS[i]}. {opt}{mark}" if i < len(LETTERS) else f"{opt}{mark}")
    return "\n".join(out)


def fmt_card(d: dict, idx: int = 0) -> str:
    code = d.get("code") or "?"
    subject = (d.get("subject") or "").upper()
    topic = d.get("topic") or ""
    chosen = d.get("chosen_idx")
    correct = d.get("correct_idx")
    explanation = (d.get("explanation") or "—").strip()
    if len(explanation) > 800:
        explanation = explanation[:800] + "…"
    return (
        f"**#{idx + 1} — {code}**"
        + (f"\n📚 {subject} • {topic}" if subject or topic else "")
        + f"\n\n{fmt_options(d.get('options') or [], correct if correct is not None else -1, chosen)}"
        + f"\n\n📖 **Explanation**\n{explanation}"
    )


class Commands:
    def __init__(self, app: Client, db: Database):
        self.app = app
        self.db = db
        self.register()

    def register(self) -> None:
        app = self.app

        @app.on_message(config.cmd_filter(["start", "help"]))
        async def _start_help(_c: Client, m: Message):
            name = m.from_user.first_name if m.from_user else "User"
            msg_text = (
                f"👋 **Hello {name}! Welcome to Quiz Practice Bot** 🧠\n\n"
                f"Is bot se aap quiz polls solve karke wrong questions tracking, "
                f"spaced repetition revision (SM-2) aur flashcards view kar sakte ho.\n\n"
                f"🛠️ **Available Commands:**\n\n"
                f"👤 **User Commands:**\n"
                f"• `/stats` — Teri accuracy, correct/attempted & streak\n"
                f"• `/wrong` — Galat kiye hue questions with explanations\n"
                f"• `/review` — Spaced Repetition (SM-2) flashcards revision\n"
                f"• `/bookmarks` — Saved wrong questions list\n"
                f"• `/bm SC12` — Question SC12 ko bookmark / unbookmark karo\n"
                f"• `/leaderboard` — Top scorers list\n\n"
                f"⚡ **Admin Commands:**\n"
                f"• `/scan @sourcechannel` — Source channel scan karke questions store karo\n"
                f"• `/gen SC1-90` — Practice channel me 90 polls generate karo\n\n"
                f"🌐 **Web Dashboard:**\n"
                f"Koyeb deploy link pe Subject → Topic → Flashcards (Flip Card) view milta hai."
            )
            await m.reply_text(msg_text)

        @app.on_message(config.cmd_filter("stats"))
        async def _stats(_c: Client, m: Message):
            if not m.from_user:
                return
            s = await self.db.user_stats(m.from_user.id)
            # subject-wise breakdown
            pipeline = [
                {"$match": {"user_id": m.from_user.id}},
                {
                    "$group": {
                        "_id": "$subject",
                        "att": {"$sum": 1},
                        "cor": {"$sum": {"$toInt": "$is_correct"}},
                    }
                },
                {"$sort": {"_id": 1}},
            ]
            rows = []
            try:
                async for r in self.db.col("sessions").aggregate(pipeline):
                    sub = (r.get("_id") or "OTHER").upper()
                    acc = (r["cor"] / r["att"] * 100) if r.get("att") else 0
                    rows.append(f"• {sub}: {r['cor']}/{r['att']} ({acc:.0f}%)")
            except Exception as e:  # noqa: BLE001
                log.debug("aggregate failed: %s", e)

            text = (
                f"📊 **Stats — {m.from_user.mention}**\n\n"
                f"🎯 Attempted: **{s['attempted']}**\n"
                f"✅ Correct: **{s['correct']}**\n"
                f"📈 Accuracy: **{s['accuracy']:.1f}%**\n"
                f"🔥 Streak: **{s['streak']}** (best {s['best_streak']})\n"
                f"🔖 Bookmarks: **{s['bookmarks']}**"
            )
            if rows:
                text += "\n\n**Subject-wise**\n" + "\n".join(rows)
            await m.reply_text(text)

        @app.on_message(config.cmd_filter("wrong"))
        async def _wrong(_c: Client, m: Message):
            if not m.from_user:
                return
            args = (m.text or "").split()
            limit = 5
            if len(args) > 1 and args[1].isdigit():
                limit = min(20, int(args[1]))
            items = await self.db.wrong_questions(m.from_user.id, limit=limit)
            if not items:
                await m.reply_text("🎉 Koi wrong question nahi hai (abhi tak)!")
                return
            for i, d in enumerate(items):
                kb = InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "🔖 Bookmark" if not d.get("bookmarked") else "✅ Bookmarked",
                                callback_data=f"bm:{d['code']}",
                            )
                        ]
                    ]
                )
                await m.reply_text(fmt_card(d, i), reply_markup=kb)
                if i < len(items) - 1:
                    import asyncio

                    await asyncio.sleep(0.3)

        @app.on_message(config.cmd_filter("bookmarks"))
        async def _bookmarks(_c: Client, m: Message):
            if not m.from_user:
                return
            items = await self.db.bookmarks(m.from_user.id, limit=20)
            if not items:
                await m.reply_text("🔖 Koi bookmark nahi hai. `/wrong` se bookmark kar sakte ho.")
                return
            for i, d in enumerate(items):
                await m.reply_text(fmt_card(d, i))
                import asyncio

                await asyncio.sleep(0.4)

        @app.on_message(config.cmd_filter("bm"))
        async def _bm(_c: Client, m: Message):
            if not m.from_user:
                return
            args = (m.text or "").split(maxsplit=1)
            if len(args) < 2:
                await m.reply_text("Usage: `/bm SC12`")
                return
            code = args[1].strip().upper()
            on = await self.db.toggle_bookmark(m.from_user.id, code)
            await m.reply_text(
                f"{'✅ Bookmarked' if on else '❌ Bookmark removed'} — `{code}`"
            )

        @app.on_callback_query(filters.regex(r"^bm:(.+)$"))
        async def _bm_cb(_c: Client, cq):
            if not cq.from_user:
                return
            code = cq.data.split(":", 1)[1]
            on = await self.db.toggle_bookmark(cq.from_user.id, code)
            await cq.answer(f"{'Bookmarked' if on else 'Removed'} {code}", show_alert=False)

        @app.on_message(config.cmd_filter("review"))
        async def _review(_c: Client, m: Message):
            if not m.from_user:
                return
            due = await self.db.srs_due(m.from_user.id, limit=10)
            if not due:
                await m.reply_text("🎉 Koi due flashcard nahi hai — sab revise ho chuke!")
                return
            for i, d in enumerate(due):
                q = await self.db.get_question(d["question_code"])
                if not q:
                    continue
                card = fmt_card(
                    {
                        "code": q["_id"],
                        "subject": q.get("subject") or d.get("subject"),
                        "topic": q.get("topic") or d.get("topic"),
                        "options": q.get("options"),
                        "correct_idx": q.get("correct_idx"),
                        "chosen_idx": None,
                        "explanation": q.get("explanation"),
                    },
                    i,
                )
                await m.reply_text(
                    f"🔁 **Spaced Review** (due"
                    f"{'' if d.get('interval') else ' — wrong earlier'})\n\n{card}"
                )

        @app.on_message(config.cmd_filter("leaderboard"))
        async def _lb(_c: Client, m: Message):
            rows = await self.db.leaderboard(limit=10)
            if not rows:
                await m.reply_text("📊 Leaderboard khali hai abhi.")
                return
            lines = []
            for i, r in enumerate(rows, 1):
                name = r.get("first_name") or r.get("username") or f"user"
                att = r.get("total_attempted") or 0
                cor = r.get("total_correct") or 0
                acc = (cor / att * 100) if att else 0
                medal = ["🥇", "🥈", "🥉"][i - 1] if i <= 3 else f"{i}."
                lines.append(f"{medal} {name} — {cor}/{att} ({acc:.0f}%)")
            await m.reply_text("🏆 **Leaderboard**\n\n" + "\n".join(lines))
