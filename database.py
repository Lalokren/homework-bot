"""
Модуль для работы с базой данных SQLite.
Хранит все сообщения из чата, чтобы бот мог выдавать Д/З за прошлые даты.
"""

import aiosqlite
from typing import List, Dict

# Имя файла базы данных
DB_PATH = "homework.db"


async def init_db() -> None:
    """Создаёт таблицу сообщений, если она ещё не существует."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                user_id INTEGER,
                username TEXT DEFAULT '',
                text TEXT DEFAULT '',
                date TEXT NOT NULL,
                has_media INTEGER DEFAULT 0,
                media_type TEXT,
                media_link TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Индекс для быстрого поиска по чату и дате
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_chat_date
            ON messages(chat_id, date)
        """)
        await db.commit()


async def save_message(msg: Dict) -> None:
    """Сохраняет одно сообщение в базу данных."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO messages
               (message_id, chat_id, user_id, username, text,
                date, has_media, media_type, media_link)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                msg["message_id"],
                msg["chat_id"],
                msg.get("user_id"),
                msg.get("username", ""),
                msg.get("text", ""),
                msg["date"],
                msg.get("has_media", 0),
                msg.get("media_type"),
                msg.get("media_link"),
            ),
        )
        await db.commit()


async def get_messages_by_date(chat_id: int, date_str: str) -> List[Dict]:
    """
    Возвращает все сообщения из указанного чата за одну дату.
    date_str — строка в формате 'ГГГГ-ММ-ДД'.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT message_id, chat_id, user_id, username, text,
                      date, has_media, media_type, media_link
               FROM messages
               WHERE chat_id = ? AND date = ?
               ORDER BY id ASC""",
            (chat_id, date_str),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]
