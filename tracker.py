"""
Tracker — continuous voter listener + auto-reveal background worker.

1. `on_poll` handler:
   Jab koi user bot ke kisi practice poll pe vote karta hai:
   - Pyrofork fires poll update (or raw update)
   - Read chosen_option_id + user identity
   - Save attempt to DB (`sessions`, `srs`, `users`)
   - Send private feedback if DM is open

2. `auto_reveal_worker`:
   Har 30 sec:
   - DB se closes_at expired polls nikalo
   - Send reveal message in practice chat with full explanation + image link
   - Mark poll `closed=True` in DB
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from pyrogram import Client, filters
from pyrogram.handlers import PollHandler
from pyrogram.types import Poll

import config
from database import Database, utcnow

log = logging.getLogger(__name__)


class Tracker:
    def __init__(self, app: Client, db: Database):
        self.app = app
        self.db = db
        self._worker_task: asyncio.Task | None = None
        self.register()

    # ─── registration ──────────────────────────────────────────────────────────
    def register(self) -> None:
        app = self.app

        @app.on_poll()
        async def _on_poll_event(cli: Client, poll: Poll):
            try:
                await self.process_poll_event(poll)
            except Exception as e:  # noqa: BLE001
                log.exception("on_poll error: %s", e)

        # Pyrofork on_poll doesn't always pass voter details (depends on MTProto update).
        # We also listen for poll_answer events if Pyrofork emits them.
        @app.on_raw_update()
        async def _on_raw(cli: Client, update, users, chats):
            name = update.__class__.__name__
            if name in ("UpdateMessagePollVote", "UpdatePoll"):
                log.debug("raw poll update: %s", name)

    # ─── poll vote processing ──────────────────────────────────────────────────
    async def process_poll_event(self, poll: Poll) -> None:
        """
        Poll update carries: id (Telegram poll id, 64-bit), chosen_option_id
        (the CURRENT user's vote — a bot gets this for polls it posted),
        and `voters` when is_anonymous=False.
        """
        if not self.db.enabled:
            return
        if poll.chosen_option_id is None:
            return  # vote withdrawn / no vote yet

        # Direct lookup by Telegram poll id — our register_poll stores it.
        p = await self.db.col("polls").find_one({"poll_id": str(poll.id), "closed": False})
        if not p:
            log.debug("poll update for unknown poll id %s — skipping", poll.id)
            return

        voters = getattr(poll, "voters", None) or []
        recorded = 0
        for v in voters:
            uid = getattr(v, "user_id", None) or getattr(v, "id", None)
            if not uid:
                continue
            await self.db.record_answer(
                user_id=uid,
                poll=p,
                chosen_idx=poll.chosen_option_id,
                first_name=getattr(v, "first_name", ""),
                username=getattr(v, "username", ""),
            )
            recorded += 1

        # Non-anonymous polls where Telegram didn't attach voters:
        # chosen_option_id belongs to the bot's own account — skip in that case.
        if recorded == 0:
            log.debug("poll %s: no voter identity attached", poll.id)

    # ─── auto-reveal background worker ─────────────────────────────────────────
    def start_worker(self) -> None:
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(self._auto_reveal_loop())

    async def _auto_reveal_loop(self) -> None:
        log.info("Auto-reveal worker started")
        while True:
            try:
                await self._check_due_polls()
            except Exception as e:  # noqa: BLE001
                log.exception("auto_reveal cycle error: %s", e)
            await asyncio.sleep(30)

    async def _check_due_polls(self) -> None:
        if not self.db.enabled:
            return
        due = await self.db.due_polls()
        for p in due:
            chat_id = p["practice_chat_id"]
            msg_id = p["poll_msg_id"]
            code = p["question_code"]

            try:
                exp = p.get("explanation") or "No explanation available"
                correct_letter = chr(65 + p.get("correct_idx", 0))  # A B C D

                # Post reveal solution message
                await self.app.send_message(
                    chat_id,
                    f"⏰ **Poll Closed — Question `{code}`**\n\n"
                    f"✅ Correct Answer: **Option ({correct_letter})**\n\n"
                    f"📖 **Explanation:**\n{exp}\n\n"
                    f"💡 *View your stats & flashcards via /stats*",
                    reply_to_message_id=msg_id,
                )
                await self.db.mark_poll_revealed(chat_id, msg_id)
                log.info("Revealed poll %s:%s for code %s", chat_id, msg_id, code)
            except Exception as e:  # noqa: BLE001
                log.error("failed to reveal poll %s:%s: %s", chat_id, msg_id, e)
                # Still mark closed to avoid infinite retry loop
                await self.db.mark_poll_closed(chat_id, msg_id)
