import os
import re
import time
import logging
import aiosqlite
from datetime import datetime
from typing import List, Dict, Any, Optional, Tuple

from src.config import (
    DB_PATH,
    LOCAL_TZ,
    DEFAULT_RETENTION_DAYS,
    MAX_STORED_TEXT_LENGTH,
    MAX_CONTEXT_MESSAGES,
)

logger = logging.getLogger(__name__)

# Словарь школьных предметов и их разговорных синонимов
SUBJECT_SYNONYMS: Dict[str, List[str]] = {
    "русский язык": ["русск", "русич", "русяз", "рус яз", "родной язык"],
    "литература": ["литерат", "литр", "литре", "чтени"],
    "математика": ["матем", "матеш", "матан"],
    "алгебра": ["алгебр"],
    "геометрия": ["геометр"],
    "английский язык": ["англ", "инглиш", "english", "англичан"],
    "физика": ["физик", "физич"],
    "химия": ["хими", "химоз"],
    "биология": ["биолог", "биол"],
    "история": ["истор"],
    "обществознание": ["обществ", "общаг"],
    "география": ["географ", "геогр"],
    "информатика": ["информ", "инфа", "инфе", "икт"],
    "обж": ["обж", "основы безопасности"],
    "черчение": ["черчен"],
}

HW_MARKERS = [
    "дз", "д/з", "домашк", "задал", "упр", "стр", "параграф", "§",
    "номер", "№", "выуч", "повтор", "прочит", "решит", "написат",
    "сочинен", "конспект", "таблиц", "пересказ", "на завтра", "на понедельник",
    "на вторник", "на среду", "на четверг", "на пятницу", "на субботу",
]


async def init_db() -> None:
    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                display_name TEXT NOT NULL,
                text TEXT NOT NULL,
                message_type TEXT NOT NULL DEFAULT 'text',
                created_at TEXT NOT NULL,
                timestamp_epoch INTEGER NOT NULL,
                UNIQUE(chat_id, message_id)
            )
            """
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_chat_time ON messages(chat_id, timestamp_epoch DESC)"
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_settings (
                chat_id INTEGER PRIMARY KEY,
                retention_days INTEGER NOT NULL DEFAULT 30
            )
            """
        )
        await db.commit()
    logger.info("База данных инициализирована: %s", DB_PATH)


async def save_or_update_message(
    chat_id: int,
    message_id: int,
    user_id: int,
    display_name: str,
    text: str,
    message_type: str = "text",
    msg_date: Optional[datetime] = None,
) -> None:
    clean_text = text.strip()[:MAX_STORED_TEXT_LENGTH]
    if not clean_text:
        return

    now_dt = msg_date.astimezone(LOCAL_TZ) if msg_date else datetime.now(LOCAL_TZ)
    formatted_time = now_dt.strftime("%d.%m.%Y %H:%M")
    epoch = int(now_dt.timestamp())

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO messages (
                chat_id, message_id, user_id, display_name, text, message_type, created_at, timestamp_epoch
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(chat_id, message_id) DO UPDATE SET
                text = excluded.text,
                display_name = excluded.display_name
            """,
            (
                chat_id,
                message_id,
                user_id,
                display_name[:80],
                clean_text,
                message_type,
                formatted_time,
                epoch,
            ),
        )
        await db.commit()


async def get_retention_days(chat_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT retention_days FROM chat_settings WHERE chat_id = ?", (chat_id,)
        )
        row = await cursor.fetchone()
        return row[0] if row else DEFAULT_RETENTION_DAYS


async def set_retention_days(chat_id: int, days: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO chat_settings (chat_id, retention_days)
            VALUES (?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET retention_days = excluded.retention_days
            """,
            (chat_id, days),
        )
        await db.commit()


async def cleanup_expired_messages(chat_id: int) -> int:
    days = await get_retention_days(chat_id)
    cutoff_epoch = int(time.time()) - (days * 86400)
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "DELETE FROM messages WHERE chat_id = ? AND timestamp_epoch < ?",
            (chat_id, cutoff_epoch),
        )
        await db.commit()
        return cursor.rowcount


async def delete_message_by_id(chat_id: int, message_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "DELETE FROM messages WHERE chat_id = ? AND message_id = ?",
            (chat_id, message_id),
        )
        await db.commit()
        return cursor.rowcount > 0


async def delete_messages_by_text(chat_id: int, substring: str) -> int:
    clean_sub = substring.strip()
    if not clean_sub:
        return 0
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "DELETE FROM messages WHERE chat_id = ? AND LOWER(text) LIKE ?",
            (chat_id, f"%{clean_sub.lower()}%"),
        )
        await db.commit()
        return cursor.rowcount


async def clear_all_chat_messages(chat_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("DELETE FROM messages WHERE chat_id = ?", (chat_id,))
        await db.commit()
        return cursor.rowcount


async def get_chat_status(chat_id: int) -> Dict[str, Any]:
    await cleanup_expired_messages(chat_id)
    retention_days = await get_retention_days(chat_id)

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            SELECT COUNT(*), MIN(created_at), MAX(created_at)
            FROM messages
            WHERE chat_id = ?
            """,
            (chat_id,),
        )
        row = await cursor.fetchone()
        count = row[0] if row else 0
        oldest = row[1] if row and row[1] else "нет записей"
        newest = row[2] if row and row[2] else "нет записей"

    return {
        "count": count,
        "oldest": oldest,
        "newest": newest,
        "retention_days": retention_days,
    }


def detect_query_subjects(query: str) -> Tuple[List[str], List[str]]:
    q_lower = query.lower()
    matched_subjects = []
    matched_stems = []

    for subject_name, stems in SUBJECT_SYNONYMS.items():
        if any(stem in q_lower for stem in stems):
            matched_subjects.append(subject_name)
            matched_stems.extend(stems)

    if "математика" in matched_subjects:
        for extra in ("алгебра", "геометрия"):
            if extra not in matched_subjects:
                matched_subjects.append(extra)
                matched_stems.extend(SUBJECT_SYNONYMS[extra])

    return matched_subjects, list(set(matched_stems))


async def search_relevant_messages(
    chat_id: int, user_query: str
) -> Tuple[List[Dict[str, Any]], List[str]]:
    await cleanup_expired_messages(chat_id)
    matched_subjects, subject_stems = detect_query_subjects(user_query)

    stop_words = {
        "юникс", "что", "было", "задали", "какое", "какая", "какие", "есть",
        "училка", "учитель", "сегодня", "завтра", "делать", "надо", "нужно",
        "домашка", "домашнее", "задание", "уроку", "предмету",
    }
    raw_words = re.findall(r"[а-яёa-z0-9]{4,}", user_query.lower())
    custom_stems = [w[:5] for w in raw_words if w not in stop_words]

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT message_id, display_name, text, message_type, created_at, timestamp_epoch
            FROM messages
            WHERE chat_id = ?
            ORDER BY timestamp_epoch DESC
            LIMIT 400
            """,
            (chat_id,),
        )
        rows = await cursor.fetchall()

    if not rows:
        return [], matched_subjects

    chronological = [dict(r) for r in reversed(rows)]
    selected_indices = set()

    for idx, msg in enumerate(chronological):
        text_lower = msg["text"].lower()
        score = 0

        if subject_stems and any(stem in text_lower for stem in subject_stems):
            score += 10

        if custom_stems and any(stem in text_lower for stem in custom_stems):
            score += 5

        has_hw_marker = any(m in text_lower for m in HW_MARKERS)
        has_numbers = bool(re.search(r"\d+", text_lower))

        if has_hw_marker:
            score += 4
        if has_numbers and (has_hw_marker or score > 0):
            score += 2

        if not subject_stems and not custom_stems:
            any_subject = any(
                stem in text_lower
                for stems in SUBJECT_SYNONYMS.values()
                for stem in stems
            )
            if has_hw_marker or any_subject:
                score += 6

        if score >= 5:
            selected_indices.add(idx)
            for next_idx in (idx + 1, idx + 2):
                if next_idx < len(chronological):
                    time_diff = (
                        chronological[next_idx]["timestamp_epoch"]
                        - msg["timestamp_epoch"]
                    )
                    if 0 <= time_diff <= 300:
                        selected_indices.add(next_idx)

    if not selected_indices and not subject_stems:
        selected_indices = set(
            range(max(0, len(chronological) - 15), len(chronological))
        )

    results = [chronological[i] for i in sorted(selected_indices)]

    if len(results) > MAX_CONTEXT_MESSAGES:
        results = results[-MAX_CONTEXT_MESSAGES:]

    return results, matched_subjects
  
