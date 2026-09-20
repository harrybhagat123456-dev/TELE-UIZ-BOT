"""
Scanner — source channel se questions nikalta hai.

Source channel layout (Science Wallah style quiz channels):

    msg N      : QUESTION  — photo (question image), caption me code "SC12"
    msg N+1..  : POLL      — reply to the question msg, type=quiz, 4 options,
                             correct_option_id set
    msg ~N+100 : EXPLANATION — reply to the POLL msg (ya question msg) with the
                               solution text

Is module ke 3 kaam:
  1. parse_poll_message()  — ek poll message se options+correct+code nikalta hai
  2. parse_code()          — kisi bhi text me "SC12" jaisa code dhoondhta hai
  3. scan_channel()        — puri history chalke questions DB me daal deta hai
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone

from pyrogram import Client
from pyrogram.types import Message, Poll

import config
from database import Database, utcnow

log = logging.getLogger(__name__)

# Code pattern: 2-4 letter prefix (letters only) + digits. e.g. SC12, PHY01, B-7
CODE_RE = re.compile(r"\b([A-Z]{1,4}[-–]?\d{1,4})\b")


def parse_code(text: str | None) -> str | None:
    """Find a quiz code like 'SC12' in text. Returns UPPERCASE code or None."""
    if not text:
        return None
    for line in text.splitlines():
        m = CODE_RE.search(line.upper())
        if m:
            return m.group(1).replace("–", "-")
    return None


def _clean(text: str | None) -> str:
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip()


class Scanner:
    def __init__(self, app: Client, db: Database):
        self.app = app
        self.db = db
        self._running = False

    # ─── single-message parsing ────────────────────────────────────────────────
    def parse_poll_message(self, msg: Message) -> dict | None:
        """Given a message containing a Poll, extract the quiz structure."""
        poll: Poll | None = getattr(msg, "poll", None)
        if poll is None:
            return None
        if not poll.options:
            return None

        options = [o.text for o in poll.options]
        correct_idx = poll.correct_option_id
        if correct_idx is None or correct_idx < 0 or correct_idx >= len(options):
            return None  # regular poll (no correct answer) — not a quiz

        # Code: poll question text, ya reply-to message ki caption/me body
        code = parse_code(poll.question) or parse_code(msg.caption) or parse_code(msg.text)
        if not code and msg.reply_to_message:
            code = parse_code(msg.reply_to_message.caption) or parse_code(
                msg.reply_to_message.text
            )

        return {
            "code": code,
            "question": _clean(poll.question),
            "options": options,
            "correct_idx": int(correct_idx),
            "poll_msg_id": msg.id,
            "chat_id": msg.chat.id,
        }

    # ─── explanation discovery ─────────────────────────────────────────────────
    async def _find_explanation(self, chat_id: int, poll_msg_id: int) -> tuple[int, str]:
        """
        Explanation = reply to the poll message (usually posted ~100 msgs later).
        Look at direct replies first, then nearby messages within the lookahead
        window that contain 'solution'/'answer' markers + the same code.
        """
        app, db = self.app, self.db

        # 1) Direct replies to the poll message.
        try:
            async for reply in app.get_chat_history(
                chat_id, reply_to_message_id=poll_msg_id
            ):
                txt = _clean(reply.caption or reply.text)
                if txt and self._looks_like_explanation(txt):
                    return reply.id, txt
        except Exception as e:  # noqa: BLE001
            log.debug("get replies failed for %s: %s", poll_msg_id, e)

        # 2) Scan forward window for explanation-ish messages.
        lookahead = max(50, config.EXPLANATION_LOOKAHEAD)
        try:
            async for msg in app.get_chat_history(chat_id, limit=lookahead):
                if msg.id <= poll_msg_id:
                    break
                txt = _clean(msg.caption or msg.text)
                if not txt:
                    continue
                if self._looks_like_explanation(txt):
                    return msg.id, txt
        except Exception as e:  # noqa: BLE001
            log.debug("forward scan failed for %s: %s", poll_msg_id, e)

        return 0, ""

    @staticmethod
    def _looks_like_explanation(text: str) -> bool:
        t = text.lower()
        markers = ("solution", "explanation", "answer", "ans:", "sol:", "explanation:")
        if any(m in t for m in markers) and len(text) > 40:
            return True
        # Solution that begins with the answer letter like "Ans (b)" / "(b) ..."
        if re.match(r"^\(?\s*ans", t) and len(text) > 30:
            return True
        return False

    # ─── main scan loop ─────────────────────────────────────────────────────────
    async def scan_channel(
        self,
        chat_id: int | str,
        limit: int = 0,
        progress_cb=None,
    ) -> dict:
        """
        Walk the channel history (newest → oldest), collect (question, poll)
        pairs, attach explanations, and upsert into the questions collection.

        Returns a summary dict.
        """
        if self._running:
            raise RuntimeError("scanner already running")
        if not self.db.enabled:
            raise RuntimeError("database not configured")

        self._running = True
        stats = {
            "scanned": 0,
            "questions": 0,
            "new": 0,
            "updated": 0,
            "no_poll": 0,
            "no_code": 0,
            "errors": 0,
        }

        try:
            chat = await self.app.get_chat(chat_id)
            chat_id = chat.id
            log.info("Scanning source channel: %s (%s)", chat.title, chat_id)

            known: set[str] = set()
            batch: list[dict] = []
            last_poll: dict | None = None

            async for msg in self.app.get_chat_history(chat_id, limit=limit or None):
                stats["scanned"] += 1
                if progress_cb and stats["scanned"] % 100 == 0:
                    try:
                        await progress_cb(stats)
                    except Exception:  # noqa: BLE001
                        pass

                # ── POLL? ───────────────────────────────────────────────────
                parsed = None
                try:
                    parsed = self.parse_poll_message(msg)
                except Exception as e:  # noqa: BLE001
                    stats["errors"] += 1
                    log.debug("poll parse error msg %s: %s", msg.id, e)

                if parsed:
                    if not parsed["code"]:
                        stats["no_code"] += 1
                        continue
                    last_poll = parsed
                    batch.append(parsed)
                    continue

                # ── QUESTION (photo with code in caption)? ───────────────────
                is_photo = bool(msg.photo)
                cap = _clean(msg.caption or msg.text)
                code = parse_code(cap)
                if is_photo and code and last_poll is None:
                    # Question with no poll found yet — register a stub.
                    doc = {
                        "_id": code,
                        "subject": "",
                        "topic": "",
                        "chat_id": chat_id,
                        "question_msg_id": msg.id,
                        "poll_msg_id": 0,
                        "question_caption": cap,
                        "options": [],
                        "correct_idx": -1,
                        "explanation": "",
                        "explanation_msg_id": 0,
                        "file_id": msg.photo.file_id if msg.photo else "",
                    }
                    if code not in known:
                        known.add(code)
                        await self.db.upsert_question(doc)
                        stats["questions"] += 1
                    continue

                # ── EXPLANATION (matches pending poll)? ──────────────────────
                if last_poll and self._looks_like_explanation(cap):
                    last_poll["explanation"] = cap
                    last_poll["explanation_msg_id"] = msg.id

                # ── flush: when we hit the question photo of the pending poll
                if last_poll and is_photo and code:
                    last_poll["question_msg_id"] = msg.id
                    last_poll["file_id"] = msg.photo.file_id if msg.photo else ""
                    last_poll["question_caption"] = cap
                    await self._flush(last_poll, chat_id, known, stats)
                    last_poll = None

            # flush trailing
            if last_poll:
                await self._flush(last_poll, chat_id, known, stats)

            stats["questions"] = len(known)
        finally:
            self._running = False

        log.info("Scan complete: %s", stats)
        return stats

    async def _flush(self, poll: dict, chat_id: int, known: set, stats: dict) -> None:
        code = poll.get("code")
        if not code:
            stats["no_code"] += 1
            return

        # Explanation not found inline → try reply-based discovery.
        if not poll.get("explanation"):
            try:
                eid, txt = await self._find_explanation(chat_id, poll["poll_msg_id"])
                poll["explanation"] = txt
                poll["explanation_msg_id"] = eid
            except Exception as e:  # noqa: BLE_RE001
                log.debug("explanation lookup failed for %s: %s", code, e)

        doc = {
            "_id": code,
            "subject": "",
            "topic": "",
            "chat_id": chat_id,
            "question_msg_id": poll.get("question_msg_id") or 0,
            "poll_msg_id": poll.get("poll_msg_id") or 0,
            "question_caption": poll.get("question_caption", ""),
            "options": poll.get("options", []),
            "correct_idx": poll.get("correct_idx", -1),
            "explanation": poll.get("explanation", ""),
            "explanation_msg_id": poll.get("explanation_msg_id") or 0,
            "file_id": poll.get("file_id", ""),
        }
        existed = code in known
        await self.db.upsert_question(doc)
        known.add(code)
        if existed:
            stats["updated"] += 1
        else:
            stats["new"] += 1


__all__ = ["Scanner", "parse_code"]
