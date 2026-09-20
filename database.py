"""
MongoDB layer — collections:

questions : one row per source-channel question
    _id             : str   unique code, e.g. "SC12"
    subject         : str   "PHYSICS" (uppercase, for grouping)
    topic           : str   "Kinematics" (free text, per subject)
    chat_id         : int   source channel id
    question_msg_id : int   the question message id (image) in source
    poll_msg_id     : int   the poll message id in source (0/None if none)
    question_caption: str   question text if any
    options         : [str] poll options (A B C D)
    correct_idx     : int   0-based correct option index
    explanation     : str   explanation text (from the later message)
    explanation_msg_id: int message id carrying the explanation
    file_id         : str   photo file_id (cached so dashboard can re-serve it)
    scanned_at      : datetime

sessions  : per-(user) answer records  (one document per attempt)
    _id             : ObjectId
    user_id         : int
    poll_msg_id     : int   practice-chat poll message id
    practice_chat_id: int
    question_code   : str   e.g. "SC12"
    chosen_idx      : int   user's answer (-1 = revote/cleared)
    correct_idx     : int
    is_correct      : bool
    bookmarked      : bool
    subject         : str
    topic           : str
    answered_at     : datetime
    created_at      : datetime

polls     : live poll registry (what the bot itself posted)
    _id             : str   f"{practice_chat_id}:{poll_msg_id}"
    practice_chat_id: int
    poll_msg_id     : int
    question_code   : str
    subject         : str
    topic           : str
    options         : [str]
    correct_idx     : int
    explanation     : str
    question_file_id: str   photo to show with reveal (optional)
    batch_id        : str   e.g. "SC1-90:1700000000"
    posted_at       : datetime
    closes_at       : datetime
    revealed        : bool
    closed          : bool   (vote window over)

users     : leaderboard / streak data
    _id             : int    user_id
    first_name      : str
    username        : str
    total_attempted : int
    total_correct   : int
    streak          : int    consecutive correct
    best_streak     : int
    last_seen       : datetime

srs       : spaced-repetition state per (user, question) — SM-2
    _id             : str   f"{user_id}:{question_code}"
    user_id         : int
    question_code   : str
    subject         : str
    topic           : str
    ease            : float  SM-2 ease factor
    interval        : int    days
    repetitions     : int
    due_at          : datetime  next review time
    last_quality    : int    last answer quality (0..5)
    updated_at      : datetime
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorCollection, AsyncIOMotorDatabase

import config

log = logging.getLogger(__name__)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Database:
    """Thin async wrapper over motor. All app state lives here."""

    def __init__(self, uri: str | None = None, db_name: str | None = None):
        self.uri = uri or config.MONGO_DB
        self.db_name = db_name or config.DB_NAME
        self.client: AsyncIOMotorClient | None = None
        self.db: AsyncIOMotorDatabase | None = None

    # ─── lifecycle ──────────────────────────────────────────────────────────────
    async def connect(self) -> None:
        if not self.uri:
            log.warning("MONGO_DB not configured — database disabled")
            return
        self.client = AsyncIOMotorClient(
            self.uri,
            serverSelectionTimeoutMS=15000,
            connectTimeoutMS=10000,
        )
        self.db = self.client[self.db_name]
        # Touch the DB so we fail fast at startup instead of on first query.
        await self.db.command("ping")
        await self._ensure_indexes()
        log.info("MongoDB connected: %s", self.db_name)

    async def close(self) -> None:
        """Close the Mongo client. Safe to call even if never connected."""
        if self.client is not None:
            self.client.close()
            self.client = None
            log.info("MongoDB connection closed")

    async def _ensure_indexes(self) -> None:
        assert self.db is not None
        await self.db.questions.create_index([("subject", 1), ("topic", 1)])
        await self.db.questions.create_index([("chat_id", 1), ("question_msg_id", 1)], unique=True)
        await self.db.sessions.create_index([("user_id", 1), ("question_code", 1), ("answered_at", -1)])
        await self.db.sessions.create_index([("user_id", 1), ("is_correct", 1), ("answered_at", -1)])
        await self.db.polls.create_index([("practice_chat_id", 1), ("poll_msg_id", 1)], unique=True)
        await self.db.polls.create_index([("closes_at", 1)])
        await self.db.polls.create_index([("batch_id", 1)])
        await self.db.srs.create_index([("user_id", 1), ("due_at", 1)])
        await self.db.srs.create_index([("user_id", 1), ("question_code", 1)], unique=True)
        await self.db.users.create_index([("total_correct", -1)])

    @property
    def enabled(self) -> bool:
        return self.db is not None

    def col(self, name: str) -> AsyncIOMotorCollection:
        if self.db is None:
            raise RuntimeError("database not connected")
        return self.db[name]

    # ─── questions (scanner) ────────────────────────────────────────────────────
    async def upsert_question(self, q: dict) -> None:
        """Insert or update a scanned question (keyed by chat_id+question_msg_id)."""
        col = self.col("questions")
        filt = {
            "chat_id": q["chat_id"],
            "question_msg_id": q["question_msg_id"],
        }
        doc = dict(q)
        doc["scanned_at"] = utcnow()
        await col.update_one(filt, {"$set": doc}, upsert=True)

    async def get_question(self, code: str) -> dict | None:
        return await self.col("questions").find_one({"_id": code.upper()})

    async def questions_by_codes(self, codes: list[str]) -> list[dict]:
        if not codes:
            return []
        cur = self.col("questions").find({"_id": {"$in": [c.upper() for c in codes]}})
        return await cur.to_list(length=None)

    async def codes_exist(self, codes: list[str]) -> set[str]:
        """Which of the given codes already exist in the DB."""
        if not codes:
            return set()
        cur = self.col("questions").find(
            {"_id": {"$in": [c.upper() for c in codes]}}, {"_id": 1}
        )
        return {d["_id"] async for d in cur}

    async def subjects(self) -> list[str]:
        return await self.col("questions").distinct("subject")

    async def topics(self, subject: str) -> list[str]:
        return await self.col("questions").distinct("topic", {"subject": subject.upper()})

    # ─── polls (generator) ──────────────────────────────────────────────────────
    async def register_poll(self, doc: dict) -> None:
        doc["_id"] = f"{doc['practice_chat_id']}:{doc['poll_msg_id']}"
        await self.col("polls").update_one(
            {"_id": doc["_id"]}, {"$set": doc}, upsert=True
        )

    async def get_poll(self, chat_id: int, msg_id: int) -> dict | None:
        return await self.col("polls").find_one({"_id": f"{chat_id}:{msg_id}"})

    async def due_polls(self) -> list[dict]:
        """Polls whose vote window has ended but not yet revealed/closed."""
        cur = self.col("polls").find(
            {"closes_at": {"$lte": utcnow()}, "closed": False}
        )
        return await cur.to_list(length=None)

    async def mark_poll_closed(self, chat_id: int, msg_id: int) -> None:
        await self.col("polls").update_one(
            {"_id": f"{chat_id}:{msg_id}"}, {"$set": {"closed": True}}
        )

    async def mark_poll_revealed(self, chat_id: int, msg_id: int) -> None:
        await self.col("polls").update_one(
            {"_id": f"{chat_id}:{msg_id}"}, {"$set": {"revealed": True, "closed": True}}
        )

    # ─── sessions (tracker / stats / bookmarks) ────────────────────────────────
    async def record_answer(
        self,
        user_id: int,
        poll: dict,
        chosen_idx: int,
        first_name: str = "",
        username: str = "",
    ) -> dict:
        """Upsert the LATEST attempt for (user, poll) and recompute user stats."""
        correct_idx = poll.get("correct_idx")
        is_correct = (
            chosen_idx is not None
            and correct_idx is not None
            and chosen_idx >= 0
            and chosen_idx == correct_idx
        )
        now = utcnow()
        doc = {
            "user_id": user_id,
            "poll_msg_id": poll["poll_msg_id"],
            "practice_chat_id": poll["practice_chat_id"],
            "question_code": poll["question_code"],
            "chosen_idx": chosen_idx,
            "correct_idx": correct_idx,
            "is_correct": bool(is_correct),
            "bookmarked": False,
            "subject": poll.get("subject", ""),
            "topic": poll.get("topic", ""),
            "answered_at": now,
            "created_at": now,
        }
        # Keep only the newest attempt per (user, poll): delete older, then insert.
        await self.col("sessions").delete_many(
            {
                "user_id": user_id,
                "poll_msg_id": poll["poll_msg_id"],
                "practice_chat_id": poll["practice_chat_id"],
            }
        )
        await self.col("sessions").insert_one(doc)

        await self._bump_user(user_id, is_correct, first_name, username)
        await self._update_srs(user_id, poll, is_correct)
        return doc

    async def _bump_user(self, user_id, is_correct, first_name, username) -> None:
        inc = {"total_attempted": 1}
        if is_correct:
            inc["total_correct"] = 1
        await self.col("users").update_one(
            {"_id": user_id},
            {
                "$set": {
                    "first_name": first_name or "",
                    "username": username or "",
                    "last_seen": utcnow(),
                },
                "$inc": inc,
                "$setOnInsert": {"streak": 0, "best_streak": 0},
            },
            upsert=True,
        )
        # Streak is a computed field — recompute from last 20 sessions for safety.
        cur = self.col("sessions").find(
            {"user_id": user_id}, {"is_correct": 1, "_id": 0}
        ).sort("answered_at", -1).limit(20)
        recent = await cur.to_list(length=20)
        streak = 0
        for r in recent:
            if r.get("is_correct"):
                streak += 1
            else:
                break
        best = await self.col("users").find_one({"_id": user_id}, {"best_streak": 1})
        best_streak = max(streak, (best or {}).get("best_streak") or 0)
        await self.col("users").update_one(
            {"_id": user_id},
            {"$set": {"streak": streak, "best_streak": best_streak}},
        )

    # ─── SM-2 spaced repetition ────────────────────────────────────────────────
    @staticmethod
    def _sm2(quality: int, ease: float, repetitions: int, interval: int) -> dict:
        """SuperMemo-2. quality 0..5 (>=3 = correct)."""
        if quality >= 3:
            if repetitions == 0:
                interval = 1
            elif repetitions == 1:
                interval = 6
            else:
                interval = max(1, round(interval * ease))
            repetitions += 1
            ease = max(1.3, ease + (0.1 - (5 - quality) * (0.08 + (5 - quality) * 0.02)))
        else:
            repetitions = 0
            interval = 0  # relearn same day
            ease = max(1.3, ease - 0.2)
        return {"ease": ease, "repetitions": repetitions, "interval": interval}

    async def _update_srs(self, user_id: int, poll: dict, is_correct: bool) -> None:
        key = f"{user_id}:{poll['question_code']}"
        cur = await self.col("srs").find_one({"_id": key})
        state = cur or {
            "ease": 2.5,
            "repetitions": 0,
            "interval": 0,
        }
        nxt = self._sm2(
            quality=5 if is_correct else 1,
            ease=float(state.get("ease", 2.5)),
            repetitions=int(state.get("repetitions", 0)),
            interval=int(state.get("interval", 0)),
        )
        from datetime import timedelta

        due = utcnow()
        if nxt["interval"] > 0:
            due = due + timedelta(days=nxt["interval"])
        else:
            due = due + timedelta(hours=1)  # wrong → review in 1 hour

        await self.col("srs").update_one(
            {"_id": key},
            {
                "$set": {
                    "user_id": user_id,
                    "question_code": poll["question_code"],
                    "subject": poll.get("subject", ""),
                    "topic": poll.get("topic", ""),
                    "ease": nxt["ease"],
                    "repetitions": nxt["repetitions"],
                    "interval": nxt["interval"],
                    "due_at": due,
                    "last_quality": 5 if is_correct else 1,
                    "updated_at": utcnow(),
                }
            },
            upsert=True,
        )

    # ─── stats / wrong questions / bookmarks ───────────────────────────────────
    async def user_stats(self, user_id: int) -> dict:
        u = await self.col("users").find_one({"_id": user_id})
        if not u:
            return {
                "attempted": 0,
                "correct": 0,
                "accuracy": 0.0,
                "streak": 0,
                "best_streak": 0,
                "bookmarks": 0,
            }
        attempted = u.get("total_attempted") or 0
        correct = u.get("total_correct") or 0
        bookmarks = await self.col("sessions").count_documents(
            {"user_id": user_id, "bookmarked": True}
        )
        return {
            "attempted": attempted,
            "attempted_cached": attempted,
            "correct": correct,
            "accuracy": (correct / attempted * 100) if attempted else 0.0,
            "streak": u.get("streak") or 0,
            "best_streak": u.get("best_streak") or 0,
            "bookmarks": bookmarks,
        }

    async def wrong_questions(self, user_id: int, limit: int = 50) -> list[dict]:
        """Latest wrong attempts joined with question data (in Python)."""
        cur = (
            self.col("sessions")
            .find({"user_id": user_id, "is_correct": False})
            .sort("answered_at", -1)
            .limit(limit)
        )
        sessions = await cur.to_list(length=limit)
        return await self._join_questions(sessions)

    async def bookmarks(self, user_id: int, limit: int = 50) -> list[dict]:
        cur = (
            self.col("sessions")
            .find({"user_id": user_id, "bookmarked": True})
           .sort("answered_at", -1)
            .limit(limit)
        )
        sessions = await cur.to_list(length=limit)
        return await self._join_questions(sessions)

    async def _join_questions(self, sessions: list[dict]) -> list[dict]:
        codes = [s["question_code"] for s in sessions if s.get("question_code")]
        questions = {}
        if codes:
            async for q in self.col("questions").find({"_id": {"$in": codes}}):
                questions[q["_id"]] = q
        out = []
        for s in sessions:
            q = questions.get(s.get("question_code") or "", {})
            out.append(
                {
                    "code": s.get("question_code"),
                    "subject": s.get("subject") or q.get("subject"),
                    "topic": s.get("topic") or q.get("topic"),
                    "options": q.get("options") or [],
                    "correct_idx": s.get("correct_idx"),
                    "chosen_idx": s.get("chosen_idx"),
                    "explanation": q.get("explanation") or "",
                    "file_id": q.get("file_id") or "",
                    "answered_at": s.get("answered_at"),
                    "bookmarked": s.get("bookmarked"),
                }
            )
        out.sort(key=lambda d: d.get("answered_at") or 0, reverse=True)
        return out

    async def set_bookmark(self, user_id: int, question_code: str, on: bool) -> None:
        await self.col("sessions").update_many(
            {"user_id": user_id, "question_code": question_code.upper()},
            {"$set": {"bookmarked": bool(on)}},
        )

    async def toggle_bookmark(self, user_id: int, question_code: str) -> bool:
        cur = await self.col("sessions").find_one(
            {"user_id": user_id, "question_code": question_code.upper()},
            {"bookmarked": 1},
        )
        on = not (cur or {}).get("bookmarked")
        await self.set_bookmark(user_id, question_code, on)
        return on

    async def leaderboard(self, limit: int = 10) -> list[dict]:
        cur = (
            self.col("users")
            .find(
                {},
                {
                    "_id": 0,
                    "first_name": 1,
                    "username": 1,
                    "total_correct": 1,
                    "total_attempted": 1,
                    "best_streak": 1,
                },
            )
            .sort("total_correct", -1)
        )
        return await cur.to_list(length=limit)

    async def srs_due(self, user_id: int, limit: int = 20) -> list[dict]:
        cur = (
            self.col("srs")
            .find({"user_id": user_id, "due_at": {"$lte": utcnow()}})
            .sort("due_at", 1)
            .sort("ease", 1)
            .limit(limit)
        )
        return await cur.to_list(length=limit)
