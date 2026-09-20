"""
Bot entrypoint — single Python process:
  • Pyrofork client (bot token)
  • Scanner (/scan)
  • Generator (/gen)
  • Tracker (poll listeners + auto-reveal worker)
  • Telegram dashboard commands (/stats /wrong /review …)
  • Flask web dashboard on PORT (Subject → Topic → flashcards)
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time

from pyrogram import Client, filters
from pyrogram.types import Message

import config
from database import Database
from scanner import Scanner, parse_code

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
# Pyrofork is chatty; keep it at WARNING unless debugging Telegram internals.
logging.getLogger("pyrogram").setLevel(logging.WARNING)
logging.getLogger("motor").setLevel(logging.WARNING)

log = logging.getLogger("quizbot")

app = Client(
    name="quiz_practice_bot",
    api_id=config.API_ID,
    api_hash=config.API_HASH,
    bot_token=config.BOT_TOKEN,
    in_memory=False,
    max_concurrent_transmissions=4,
)

db = Database()


async def main() -> None:
    log.info("=== Quiz Practice Bot — starting ===")

    # 1) DB
    await db.connect()

    # 2) Scanner (commands registered below)
    scanner = Scanner(app, db)

    @app.on_message(filters.command("scan") & filters.user(list(config.ADMIN_IDS)))
    async def _scan(_cli: Client, msg: Message):
        args = (msg.text or "").split(maxsplit=1)
        chats = []
        if len(args) > 1:
            chats = [c for c in args[1].split() if c]
        if not chats:
            chats = list(config.SOURCE_CHATS)
        if not chats:
            await msg.reply_text(
                "Usage: `/scan @sourcechannel`\n"
                "Ya env me `SOURCE_CHATS` set karo."
            )
            return

        status = await msg.reply_text(f"🔎 Scanning {len(chats)} channel(s)…")

        async def progress(stats: dict) -> None:
            await status.edit_text(
                f"🔎 Scanning…\n"
                f"Messages: {stats['scanned']}\n"
                f"Questions found: {stats['questions']}\n"
                f"New: {stats['new']} | Updated: {stats['updated']}"
            )

        total = {
            "scanned": 0,
            "questions": 0,
            "new": 0,
            "updated": 0,
            "no_poll": 0,
            "no_code": 0,
            "errors": 0,
        }
        for chat in chats:
            try:
                res = await scanner.scan_channel(chat, progress_cb=progress)
                for k in total:
                    total[k] += res.get(k, 0)
            except Exception as e:  # noqa: BLE001
                log.exception("scan failed for %s: %s", chat, e)
                await msg.reply_text(f"❌ Scan failed for {chat}: {e}")

        await status.edit_text(
            f"✅ **Scan complete**\n\n"
            f"Messages scanned: **{total['scanned']}**\n"
            f"Questions found: **{total['questions']}**\n"
            f"New: **{total['new']}**  |  Updated: **{total['updated']}**\n"
            f"No poll: {total['no_poll']} | No code: {total['no_code']} | "
            f"Errors: {total['errors']}"
        )

    # 3) Generator, Tracker, Commands
    from generator import Generator
    from tracker import Tracker
    from commands import Commands

    generator = Generator(app, db)
    tracker = Tracker(app, db)
    commands = Commands(app, db)

    # 4) Start client first (needed for everything else)
    await app.start()
    log.info("Pyrofork client started")

    me = await app.get_me()
    log.info("Bot: @%s (%s)", me.username, me.id)

    # 5) Background worker
    tracker.start_worker()

    # 6) Web dashboard (Koyeb health check + flashcards UI)
    from web import start_web

    start_web(db, in_thread=True)

    log.info("=== Bot ready. Commands: /scan /gen /stats /wrong /review ===")

    # Keep running forever
    await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("shutdown")
