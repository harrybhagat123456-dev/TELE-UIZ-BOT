"""
Generator — `/gen SC1-90` command.

Flow:
  1. Owner/admin runs /gen <range>
  2. Bot asks SUBJECT  (inline keyboard: PHYSICS / CHEMISTRY / BIOLOGY / custom)
  3. Bot asks TOPIC    (inline keyboard: known topics for that subject, + custom)
  4. Bot fetches all questions in range from DB
  5. Posts one QUIZ poll per question to PRACTICE_CHAT:
       - photo (question image) as a reply target, then the poll
       - type=quiz, correct_option_id, explanation, open_period
     Polls are open for POLL_OPEN_SECONDS (Telegram max 600s), and the bot
     marks the vote window closed after VOTE_WINDOW_SECONDS.
  6. Poll updates (votes) are handled by tracker.py
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone

from pyrogram import Client
from pyrogram import enums, filters
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

import config
from database import Database, utcnow

log = logging.getLogger(__name__)

SUBJECTS = ["PHYSICS", "CHEMISTRY", "BIOLOGY", "MATHS", "OTHER"]

# In-memory FSM for the /gen conversation: user_id -> {step, range, subject}
_gen_state: dict[int, dict] = {}

BATCH_KEY = lambda code_range, ts: f"{code_range}:{ts}"  # noqa: E731


def parse_range(spec: str) -> list[str] | None:
    """
    'SC1-90'      -> ['SC1','SC2',...,'SC90']
    'SC12'        -> ['SC12']
    'SC1,SC5,SC9' -> ['SC1','SC5','SC9']
    """
    spec = (spec or "").strip().upper().replace("–", "-").replace(" ", "")
    if not spec:
        return None

    if "," in spec:
        out, seen = [], set()
        for part in spec.split(","):
            r = parse_range(part)
            if r:
                for c in r:
                    if c not in seen:
                        seen.add(c)
                        out.append(c)
        return out or None

    m = filters.command  # placeholder removed — real parse below
    import re

    m = re.match(r"^([A-Z]{1,4})(\d{1,4})(?:-(\d{1,4}))?$", spec)
    if not m:
        return None
    prefix, start, end = m.group(1), int(m.group(2)), m.group(3)
    if end is None:
        return [f"{prefix}{start}"]
    end = int(end)
    if end < start:
        return None
    width = len(m.group(2))  # keep zero padding consistent
    return [f"{prefix}{str(i).zfill(width)}" for i in range(start, end + 1)]


class Generator:
    def __init__(self, app: Client, db: Database):
        self.app = app
        self.db = db
        self._posting: set[str] = set()  # batch_ids in flight
        self.register()

    # ─── registration ──────────────────────────────────────────────────────────
    def register(self) -> None:
        app = self.app

        @app.on_message(config.cmd_filter(["gen", "generate"]))
        async def _gen_cmd(_cli: Client, msg: Message):
            if config.ADMIN_IDS and msg.from_user and msg.from_user.id not in config.ADMIN_IDS:
                await msg.reply_text(
                    "⛔ **Access denied** — sirf admins hi polls generate kar sakte hain."
                )
                return
            if not config.ADMIN_IDS:
                await msg.reply_text(
                    "⚠️ OWNER_ID environment variable set nahi hai!\n"
                    "Apna Telegram user ID `OWNER_ID=123456789` ke roop me set karo."
                )
                return
            if not config.PRACTICE_CHAT:
                await msg.reply_text(
                    "⚠️ PRACTICE_CHAT set nahi hai — polls kahan post karne hain?"
                )
                return
            args = (msg.text or "").split(maxsplit=1)
            if len(args) < 2:
                await msg.reply_text(
                    "Usage: `/gen SC1-90`\n"
                    "Range: `SC1-90` (SC1 se SC90), single: `SC12`, "
                    "list: `SC1,SC5,SC9`",
                )
                return
            spec = args[1].strip()
            codes = parse_range(spec)
            if not codes:
                await msg.reply_text(f"❌ Range samajh nahi aaya: `{spec}`")
                return

            _gen_state[msg.from_user.id] = {
                "step": "subject",
                "spec": spec,
                "codes": codes,
                "subject": None,
                "topic": None,
            }
            await self._ask_subject(msg)

        @app.on_callback_query(filters.regex(r"^gen_subj:(.+)$"))
        async def _gen_subject_cb(_cli: Client, cq: CallbackQuery):
            state = _gen_state.get(cq.from_user.id)
            if not state:
                await cq.answer("Session expired — /gen dobara chalao", show_alert=True)
                return
            subject = cq.data.split(":", 1)[1]
            state["subject"] = subject
            state["step"] = "topic"
            await cq.message.edit_text(
                f"📚 Subject: **{subject}**\n\n"
                f"Ab **topic** choose kar ({len(state['codes'])} questions):"
            )
            await self._ask_topic(cq, subject)

        @app.on_callback_query(filters.regex(r"^gen_topic:(.*)$"))
        async def _gen_topic_cb(_cli: Client, cq: CallbackQuery):
            state = _gen_state.get(cq.from_user.id)
            if not state:
                await cq.answer("Session expired — /gen dobara chalao", show_alert=True)
                return
            topic = cq.data.split(":", 1)[1] or "General"
            state["topic"] = topic
            state["step"] = "run"
            await cq.message.delete()
            await self.run_generation(
                chat=msg_chat(cq),
                admin_id=cq.from_user.id,
                subject=state["subject"],
                topic=topic,
                codes=state["codes"],
                spec=state["spec"],
            )

    # ─── conversation helpers ──────────────────────────────────────────────────
    async def _ask_subject(self, msg: Message) -> None:
        rows = [
            [InlineKeyboardButton(s, callback_data=f"gen_subj:{s}") for s in SUBJECTS[i : i + 3]]
            for i in range(0, len(SUBJECTS), 3)
        ]
        await msg.reply_text(
            f"🎯 Range: **{parse_spec_text(msg)}**\n\n"
            f"Sabse pehle **subject** choose kar:",
            reply_markup=InlineKeyboardMarkup(rows),
        )

    async def _ask_topic(self, cq: CallbackQuery, subject: str) -> None:
        topics = []
        try:
            topics = await self.db.topics(subject)
        except Exception as e:  # noqa: BLE001
            log.debug("topics lookup failed: %s", e)
        rows = []
        if topics:
            for i in range(0, min(len(topics), 8), 2):
                rows.append(
                    [
                        InlineKeyboardButton(
                            t, callback_data=f"gen_topic:{t}"
                        )
                        for t in topics[i : i + 2]
                    ]
                )
        rows.append([InlineKeyboardButton("✍️ Custom topic", callback_data="gen_topic:")])
        await cq.message.reply_text(
            f"📖 **Topic** choose kar:",
            reply_markup=InlineKeyboardMarkup(rows),
        )

    # ─── generation ────────────────────────────────────────────────────────────
    async def run_generation(
        self,
        chat,
        admin_id: int,
        subject: str,
        topic: str,
        codes: list[str],
        spec: str,
    ) -> None:
        status = await self.app.send_message(
            chat.id if hasattr(chat, "id") else chat,
            f"⏳ **{subject} • {topic}**\n"
            f"Range: `{spec}` → {len(codes)} questions\n"
            f"DB se fetch ho rahe hain...",
        )

        questions = await self.db.questions_by_codes(codes)
        found = {q["_id"] for q in questions}
        missing = [c for c in codes if c not in found]

        ready = [q for q in questions if q.get("correct_idx", -1) in range(0, 4)]
        if not ready:
            await status.edit_text(
                f"❌ Koi post karne layak question nahi mila.\n"
                f"Missing: {', '.join(missing[:20]) or '—'}"
            )
            return

        batch_id = BATCH_KEY(spec, int(time.time()))
        self._posting.add(batch_id)
        closes_at = utcnow() + timedelta(seconds=config.VOTE_WINDOW_SECONDS)

        await status.edit_text(
            f" ✅ **{subject} • {topic}**\n"
            f"Posting {len(ready)} polls…\n"
            f"Missing codes: {len(missing)}\n"
            f"Batch: `{batch_id}`"
        )

        posted = 0
        errors = 0
        practice_chat = config.PRACTICE_CHAT

        for i, q in enumerate(ready, 1):
            try:
                await self._post_one(
                    practice_chat, q, subject, topic, batch_id, closes_at, i, len(ready)
                )
                posted += 1
            except Exception as e:  # noqa: BLE001
                errors += 1
                log.exception("post failed for %s: %s", q.get("_id"), e)
                # FloodWait ke liye thoda ruk
                if "FLOOD" in str(e).upper():
                    await asyncio.sleep(10)
            await asyncio.sleep(config.POLL_SEND_GAP)

        self._posting.discard(batch_id)
        await status.edit_text(
            f"🎉 **Done!** {subject} • {topic}\n"
            f"Posted: **{posted}** polls  |  Errors: {errors}\n"
            f"Missing: {len(missing)}"
            + (f"\nMissing codes: {', '.join(missing[:15])}" if missing else ""),
        )

    async def _post_one(
        self,
        chat_id,
        q: dict,
        subject: str,
        topic: str,
        batch_id: str,
        closes_at,
        idx: int,
        total: int,
    ) -> None:
        app = self.app
        options = q.get("options") or []
        correct_idx = q.get("correct_idx", -1)
        if not (0 <= correct_idx < len(options)):
            raise ValueError(f"bad options for {q.get('_id')}")

        # 1) Question photo (agar hai) — poll is reply to it
        photo_msg = None
        file_id = q.get("file_id") or ""
        if file_id:
            try:
                photo_msg = await app.send_photo(
                    chat_id,
                    file_id,
                    caption=(
                        f"**{subject} • {topic}**\n"
                        f"Question `{q['_id']}`  ({idx}/{total})\n"
                        f"⏱️ {config.POLL_OPEN_SECONDS // 60} min"
                    ),
                )
            except Exception as e:  # noqa: BLE001
                log.debug("photo post failed, continuing: %s", e)
                photo_msg = None

        # 2) The quiz poll
        question_text = q.get("question_caption") or q.get("question") or ""
        question = f"Q {q['_id']}" if not question_text else question_text
        poll_msg = await app.send_poll(
            chat_id,
            question=question[:250],
            options=options,
            type=enums.PollType.QUIZ,
            correct_option_id=correct_idx,
            explanation=(q.get("explanation") or "—")[:200],
            open_period=config.POLL_OPEN_SECONDS,
            is_anonymous=False,
            reply_to_message_id=photo_msg.id if photo_msg else None,
        )

        await self.db.register_poll(
            {
                "practice_chat_id": poll_msg.chat.id,
                "poll_id": str(poll_msg.poll.id),
                "poll_msg_id": poll_msg.id,
                "question_code": q["_id"],
                "subject": subject,
                "topic": topic,
                "options": options,
                "correct_idx": correct_idx,
                "explanation": q.get("explanation") or "",
                "question_file_id": file_id,
                "batch_id": batch_id,
                "posted_at": utcnow(),
                "closes_at": closes_at,
                "revealed": False,
                "closed": False,
            }
        )


def parse_spec_text(msg: Message) -> str:
    args = (msg.text or "").split(maxsplit=1)
    return args[1].strip() if len(args) > 1 else ""


def msg_chat(cq: CallbackQuery):
    return cq.message.chat if cq.message else None
