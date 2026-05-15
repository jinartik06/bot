from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def dumps(value: Any) -> str:
    return json.dumps(value or [], ensure_ascii=False)


def loads(value: str | None) -> Any:
    if not value:
        return []
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return []


@dataclass
class User:
    telegram_id: int
    username: str | None
    display_name: str | None
    timezone: str
    digest_enabled: bool
    digest_weekday: int
    digest_time: str
    is_admin: bool


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute("PRAGMA foreign_keys = ON")
        await self.migrate()

    async def close(self) -> None:
        if self.conn:
            await self.conn.close()

    @property
    def db(self) -> aiosqlite.Connection:
        if not self.conn:
            raise RuntimeError("Database is not connected")
        return self.conn

    async def fetchone(self, query: str, params: tuple = ()) -> aiosqlite.Row | None:
        cur = await self.db.execute(query, params)
        return await cur.fetchone()

    async def migrate(self) -> None:
        await self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS allowed_users (
                telegram_id INTEGER PRIMARY KEY,
                username TEXT,
                is_admin INTEGER NOT NULL DEFAULT 0,
                added_by INTEGER,
                added_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                username TEXT,
                display_name TEXT,
                timezone TEXT NOT NULL,
                digest_enabled INTEGER NOT NULL DEFAULT 1,
                digest_weekday INTEGER NOT NULL DEFAULT 6,
                digest_time TEXT NOT NULL DEFAULT '19:00',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS blocked_users (
                telegram_id INTEGER PRIMARY KEY,
                username TEXT,
                reason TEXT,
                blocked_by INTEGER,
                blocked_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(user_id, name),
                FOREIGN KEY(user_id) REFERENCES users(telegram_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS ideas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                summary TEXT,
                tldr TEXT,
                full_text TEXT,
                key_points_json TEXT NOT NULL DEFAULT '[]',
                open_questions_json TEXT NOT NULL DEFAULT '[]',
                next_step TEXT,
                side_thoughts TEXT,
                category_id INTEGER,
                tags_json TEXT NOT NULL DEFAULT '[]',
                original_text TEXT NOT NULL,
                source_type TEXT NOT NULL,
                priority_fire INTEGER NOT NULL DEFAULT 0,
                pinned_at TEXT,
                pinned_chat_id INTEGER,
                pinned_message_id INTEGER,
                photo_file_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(telegram_id) ON DELETE CASCADE,
                FOREIGN KEY(category_id) REFERENCES categories(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS digest_runs (
                user_id INTEGER NOT NULL,
                digest_key TEXT NOT NULL,
                sent_at TEXT NOT NULL,
                PRIMARY KEY(user_id, digest_key),
                FOREIGN KEY(user_id) REFERENCES users(telegram_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS processed_messages (
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                processed_at TEXT NOT NULL,
                PRIMARY KEY(chat_id, message_id)
            );
            """
        )
        await self.ensure_column("ideas", "pinned_at", "TEXT")
        await self.ensure_column("ideas", "pinned_chat_id", "INTEGER")
        await self.ensure_column("ideas", "pinned_message_id", "INTEGER")
        await self.db.commit()

    async def ensure_column(self, table: str, column: str, definition: str) -> None:
        cur = await self.db.execute(f"PRAGMA table_info({table})")
        columns = {row["name"] for row in await cur.fetchall()}
        if column not in columns:
            await self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    async def seed_allowed(self, allowed_ids: set[int], admin_ids: set[int]) -> None:
        now = utc_now()
        for telegram_id in allowed_ids | admin_ids:
            await self.db.execute(
                """
                INSERT INTO allowed_users (telegram_id, is_admin, added_at)
                VALUES (?, ?, ?)
                ON CONFLICT(telegram_id) DO UPDATE SET is_admin = excluded.is_admin
                """,
                (telegram_id, 1 if telegram_id in admin_ids else 0, now),
            )
        await self.db.commit()

    async def is_allowed(self, telegram_id: int) -> bool:
        row = await self.fetchone(
            "SELECT 1 FROM allowed_users WHERE telegram_id = ?",
            (telegram_id,),
        )
        return row is not None

    async def is_blocked(self, telegram_id: int) -> bool:
        row = await self.fetchone(
            "SELECT 1 FROM blocked_users WHERE telegram_id = ?",
            (telegram_id,),
        )
        return row is not None

    async def is_admin(self, telegram_id: int) -> bool:
        row = await self.fetchone(
            "SELECT is_admin FROM allowed_users WHERE telegram_id = ?",
            (telegram_id,),
        )
        return bool(row and row["is_admin"])

    async def upsert_user(
        self,
        telegram_id: int,
        username: str | None,
        display_name: str | None,
        default_timezone: str,
        default_weekday: int,
        default_time: str,
    ) -> None:
        now = utc_now()
        await self.db.execute(
            """
            INSERT INTO users (
                telegram_id, username, display_name, timezone,
                digest_weekday, digest_time, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                username = excluded.username,
                updated_at = excluded.updated_at
            """,
            (telegram_id, username, display_name, default_timezone, default_weekday, default_time, now, now),
        )
        await self.db.commit()

    async def update_display_name(self, telegram_id: int, display_name: str) -> None:
        await self.db.execute(
            "UPDATE users SET display_name = ?, updated_at = ? WHERE telegram_id = ?",
            (display_name, utc_now(), telegram_id),
        )
        await self.db.commit()

    async def get_user(self, telegram_id: int) -> User | None:
        row = await self.fetchone(
            """
            SELECT u.*, COALESCE(a.is_admin, 0) AS is_admin
            FROM users u
            LEFT JOIN allowed_users a ON a.telegram_id = u.telegram_id
            WHERE u.telegram_id = ?
            """,
            (telegram_id,),
        )
        if not row:
            return None
        return User(
            telegram_id=row["telegram_id"],
            username=row["username"],
            display_name=row["display_name"],
            timezone=row["timezone"],
            digest_enabled=bool(row["digest_enabled"]),
            digest_weekday=row["digest_weekday"],
            digest_time=row["digest_time"],
            is_admin=bool(row["is_admin"]),
        )

    async def list_allowed(self) -> list[aiosqlite.Row]:
        cur = await self.db.execute(
            "SELECT * FROM allowed_users ORDER BY is_admin DESC, added_at DESC"
        )
        return await cur.fetchall()

    async def list_blocked(self) -> list[aiosqlite.Row]:
        cur = await self.db.execute(
            """
            SELECT b.*, u.display_name
            FROM blocked_users b
            LEFT JOIN users u ON u.telegram_id = b.telegram_id
            ORDER BY b.blocked_at DESC
            """
        )
        return await cur.fetchall()

    async def list_admin_users(self, limit: int = 80) -> list[aiosqlite.Row]:
        cur = await self.db.execute(
            """
            SELECT
                u.telegram_id,
                u.username,
                u.display_name,
                u.updated_at,
                COALESCE(a.is_admin, 0) AS is_admin,
                CASE WHEN a.telegram_id IS NULL THEN 0 ELSE 1 END AS is_allowed,
                CASE WHEN b.telegram_id IS NULL THEN 0 ELSE 1 END AS is_blocked,
                b.reason AS block_reason,
                COUNT(i.id) AS ideas_count
            FROM users u
            LEFT JOIN allowed_users a ON a.telegram_id = u.telegram_id
            LEFT JOIN blocked_users b ON b.telegram_id = u.telegram_id
            LEFT JOIN ideas i ON i.user_id = u.telegram_id
            GROUP BY u.telegram_id
            ORDER BY is_blocked DESC, u.updated_at DESC
            LIMIT ?
            """,
            (limit,),
        )
        rows = list(await cur.fetchall())
        seen_ids = {row["telegram_id"] for row in rows}
        if len(rows) >= limit:
            return rows

        cur = await self.db.execute(
            """
            SELECT
                a.telegram_id AS telegram_id,
                COALESCE(a.username, b.username) AS username,
                NULL AS display_name,
                COALESCE(b.blocked_at, a.added_at) AS updated_at,
                COALESCE(a.is_admin, 0) AS is_admin,
                1 AS is_allowed,
                CASE WHEN b.telegram_id IS NULL THEN 0 ELSE 1 END AS is_blocked,
                b.reason AS block_reason,
                0 AS ideas_count
            FROM allowed_users a
            LEFT JOIN blocked_users b ON b.telegram_id = a.telegram_id
            WHERE a.telegram_id NOT IN (
                SELECT telegram_id FROM users
            )
            UNION ALL
            SELECT
                b.telegram_id AS telegram_id,
                b.username AS username,
                NULL AS display_name,
                b.blocked_at AS updated_at,
                0 AS is_admin,
                0 AS is_allowed,
                1 AS is_blocked,
                b.reason AS block_reason,
                0 AS ideas_count
            FROM blocked_users b
            LEFT JOIN allowed_users a ON a.telegram_id = b.telegram_id
            WHERE a.telegram_id IS NULL
              AND b.telegram_id NOT IN (
                SELECT telegram_id FROM users
              )
            ORDER BY is_blocked DESC, updated_at DESC
            LIMIT ?
            """,
            (limit - len(rows),),
        )
        extra_rows = [row for row in await cur.fetchall() if row["telegram_id"] not in seen_ids]
        return rows + extra_rows

    async def add_allowed(self, telegram_id: int, username: str | None, is_admin: bool, added_by: int) -> None:
        await self.db.execute(
            """
            INSERT INTO allowed_users (telegram_id, username, is_admin, added_by, added_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET username = excluded.username, is_admin = excluded.is_admin
            """,
            (telegram_id, username, 1 if is_admin else 0, added_by, utc_now()),
        )
        await self.db.commit()

    async def remove_allowed(self, telegram_id: int) -> None:
        await self.db.execute("DELETE FROM allowed_users WHERE telegram_id = ?", (telegram_id,))
        await self.db.commit()

    async def block_user(self, telegram_id: int, username: str | None, reason: str | None, blocked_by: int) -> None:
        await self.db.execute(
            """
            INSERT INTO blocked_users (telegram_id, username, reason, blocked_by, blocked_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                username = COALESCE(excluded.username, blocked_users.username),
                reason = excluded.reason,
                blocked_by = excluded.blocked_by,
                blocked_at = excluded.blocked_at
            """,
            (telegram_id, username, reason, blocked_by, utc_now()),
        )
        await self.db.commit()

    async def unblock_user(self, telegram_id: int) -> None:
        await self.db.execute("DELETE FROM blocked_users WHERE telegram_id = ?", (telegram_id,))
        await self.db.commit()

    async def ensure_category(self, user_id: int, name: str) -> int:
        clean = name.strip()[:80] or "Без категории"
        await self.db.execute(
            "INSERT OR IGNORE INTO categories (user_id, name, created_at) VALUES (?, ?, ?)",
            (user_id, clean, utc_now()),
        )
        await self.db.commit()
        row = await self.fetchone(
            "SELECT id FROM categories WHERE user_id = ? AND name = ?",
            (user_id, clean),
        )
        return int(row["id"])

    async def list_categories(self, user_id: int) -> list[aiosqlite.Row]:
        cur = await self.db.execute(
            """
            SELECT c.id, c.name, COUNT(i.id) AS ideas_count
            FROM categories c
            LEFT JOIN ideas i ON i.category_id = c.id
            WHERE c.user_id = ?
            GROUP BY c.id
            ORDER BY c.name
            """,
            (user_id,),
        )
        return await cur.fetchall()

    async def create_idea(self, user_id: int, payload: dict[str, Any], original_text: str, source_type: str, photo_file_id: str | None) -> int:
        category_id = await self.ensure_category(user_id, payload.get("category") or "Без категории")
        now = utc_now()
        cur = await self.db.execute(
            """
            INSERT INTO ideas (
                user_id, title, summary, tldr, full_text, key_points_json,
                open_questions_json, next_step, side_thoughts, category_id,
                tags_json, original_text, source_type, priority_fire, pinned_at,
                pinned_chat_id, pinned_message_id, photo_file_id, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                (payload.get("title") or "Новая идея").strip()[:180],
                payload.get("summary"),
                payload.get("tldr"),
                payload.get("full_text"),
                dumps(payload.get("key_points")),
                dumps(payload.get("open_questions")),
                payload.get("next_step"),
                payload.get("side_thoughts"),
                category_id,
                dumps(payload.get("tags")),
                original_text,
                source_type,
                0,
                None,
                None,
                None,
                photo_file_id,
                now,
                now,
            ),
        )
        await self.db.commit()
        return int(cur.lastrowid)

    async def mark_message_processed(self, chat_id: int, message_id: int, user_id: int) -> bool:
        cur = await self.db.execute(
            """
            INSERT OR IGNORE INTO processed_messages (chat_id, message_id, user_id, processed_at)
            VALUES (?, ?, ?, ?)
            """,
            (chat_id, message_id, user_id, utc_now()),
        )
        await self.db.commit()
        return cur.rowcount > 0

    async def get_idea(self, user_id: int, idea_id: int) -> aiosqlite.Row | None:
        return await self.fetchone(
            """
            SELECT i.*, c.name AS category
            FROM ideas i
            LEFT JOIN categories c ON c.id = i.category_id
            WHERE i.user_id = ? AND i.id = ?
            """,
            (user_id, idea_id),
        )

    async def list_ideas(self, user_id: int, limit: int = 10) -> list[aiosqlite.Row]:
        cur = await self.db.execute(
            """
            SELECT i.*, c.name AS category
            FROM ideas i
            LEFT JOIN categories c ON c.id = i.category_id
            WHERE i.user_id = ?
            ORDER BY i.created_at DESC
            LIMIT ?
            """,
            (user_id, limit),
        )
        return await cur.fetchall()

    async def latest_idea(self, user_id: int) -> aiosqlite.Row | None:
        return await self.fetchone(
            """
            SELECT i.*, c.name AS category
            FROM ideas i
            LEFT JOIN categories c ON c.id = i.category_id
            WHERE i.user_id = ?
            ORDER BY i.created_at DESC
            LIMIT 1
            """,
            (user_id,),
        )

    async def search_ideas(self, user_id: int, query: str) -> list[aiosqlite.Row]:
        like = f"%{query}%"
        cur = await self.db.execute(
            """
            SELECT i.*, c.name AS category
            FROM ideas i
            LEFT JOIN categories c ON c.id = i.category_id
            WHERE i.user_id = ?
              AND (
                i.title LIKE ? OR i.summary LIKE ? OR i.tldr LIKE ? OR
                i.full_text LIKE ? OR i.original_text LIKE ? OR i.tags_json LIKE ?
              )
            ORDER BY i.created_at DESC
            LIMIT 20
            """,
            (user_id, like, like, like, like, like, like),
        )
        return await cur.fetchall()

    async def ideas_since(self, user_id: int, since_iso: str) -> list[aiosqlite.Row]:
        cur = await self.db.execute(
            """
            SELECT i.*, c.name AS category
            FROM ideas i
            LEFT JOIN categories c ON c.id = i.category_id
            WHERE i.user_id = ? AND i.created_at >= ?
            ORDER BY i.created_at DESC
            """,
            (user_id, since_iso),
        )
        return await cur.fetchall()

    async def ideas_by_category(self, user_id: int, category_id: int) -> list[aiosqlite.Row]:
        cur = await self.db.execute(
            """
            SELECT i.*, c.name AS category
            FROM ideas i
            LEFT JOIN categories c ON c.id = i.category_id
            WHERE i.user_id = ? AND i.category_id = ?
            ORDER BY i.created_at DESC
            """,
            (user_id, category_id),
        )
        return await cur.fetchall()

    async def delete_idea(self, user_id: int, idea_id: int) -> None:
        await self.db.execute("DELETE FROM ideas WHERE user_id = ? AND id = ?", (user_id, idea_id))
        await self.db.commit()

    async def pin_idea(self, user_id: int, idea_id: int, chat_id: int, message_id: int) -> None:
        now = utc_now()
        await self.db.execute(
            """
            UPDATE ideas
            SET pinned_at = ?,
                pinned_chat_id = ?,
                pinned_message_id = ?,
                updated_at = ?
            WHERE user_id = ? AND id = ?
            """,
            (now, chat_id, message_id, now, user_id, idea_id),
        )
        await self.db.commit()

    async def update_title(self, user_id: int, idea_id: int, title: str) -> None:
        await self.db.execute(
            "UPDATE ideas SET title = ?, updated_at = ? WHERE user_id = ? AND id = ?",
            (title.strip()[:180], utc_now(), user_id, idea_id),
        )
        await self.db.commit()

    async def update_idea_category(self, user_id: int, idea_id: int, category_name: str) -> None:
        category_id = await self.ensure_category(user_id, category_name)
        await self.db.execute(
            "UPDATE ideas SET category_id = ?, updated_at = ? WHERE user_id = ? AND id = ?",
            (category_id, utc_now(), user_id, idea_id),
        )
        await self.db.commit()

    async def update_idea_analysis(self, user_id: int, idea_id: int, payload: dict[str, Any]) -> None:
        await self.db.execute(
            """
            UPDATE ideas
            SET title = ?,
                summary = ?,
                tldr = ?,
                full_text = ?,
                key_points_json = ?,
                open_questions_json = ?,
                next_step = ?,
                side_thoughts = ?,
                tags_json = ?,
                updated_at = ?
            WHERE user_id = ? AND id = ?
            """,
            (
                (payload.get("title") or "Новая мысль").strip()[:180],
                payload.get("summary"),
                payload.get("tldr"),
                payload.get("full_text"),
                dumps(payload.get("key_points")),
                dumps(payload.get("open_questions")),
                payload.get("next_step"),
                payload.get("side_thoughts"),
                dumps(payload.get("tags")),
                utc_now(),
                user_id,
                idea_id,
            ),
        )
        await self.db.commit()

    async def update_settings(self, user_id: int, **kwargs: Any) -> None:
        allowed = {"timezone", "digest_enabled", "digest_weekday", "digest_time"}
        pairs = [(key, value) for key, value in kwargs.items() if key in allowed]
        if not pairs:
            return
        set_clause = ", ".join(f"{key} = ?" for key, _ in pairs)
        values = [value for _, value in pairs]
        values.extend([utc_now(), user_id])
        await self.db.execute(
            f"UPDATE users SET {set_clause}, updated_at = ? WHERE telegram_id = ?",
            values,
        )
        await self.db.commit()

    async def users_for_digest(self, weekday: int, hhmm: str) -> list[User]:
        cur = await self.db.execute(
            """
            SELECT u.*, COALESCE(a.is_admin, 0) AS is_admin
            FROM users u
            LEFT JOIN allowed_users a ON a.telegram_id = u.telegram_id
            WHERE u.digest_enabled = 1 AND u.digest_weekday = ? AND u.digest_time = ?
            """,
            (weekday, hhmm),
        )
        rows = await cur.fetchall()
        return [
            User(
                telegram_id=row["telegram_id"],
                username=row["username"],
                display_name=row["display_name"],
                timezone=row["timezone"],
                digest_enabled=bool(row["digest_enabled"]),
                digest_weekday=row["digest_weekday"],
                digest_time=row["digest_time"],
                is_admin=bool(row["is_admin"]),
            )
            for row in rows
        ]

    async def all_digest_users(self) -> list[User]:
        cur = await self.db.execute(
            """
            SELECT u.*, COALESCE(a.is_admin, 0) AS is_admin
            FROM users u
            LEFT JOIN allowed_users a ON a.telegram_id = u.telegram_id
            WHERE u.digest_enabled = 1
            """
        )
        rows = await cur.fetchall()
        return [
            User(
                telegram_id=row["telegram_id"],
                username=row["username"],
                display_name=row["display_name"],
                timezone=row["timezone"],
                digest_enabled=bool(row["digest_enabled"]),
                digest_weekday=row["digest_weekday"],
                digest_time=row["digest_time"],
                is_admin=bool(row["is_admin"]),
            )
            for row in rows
        ]

    async def has_digest_run(self, user_id: int, digest_key: str) -> bool:
        row = await self.fetchone(
            "SELECT 1 FROM digest_runs WHERE user_id = ? AND digest_key = ?",
            (user_id, digest_key),
        )
        return row is not None

    async def mark_digest_sent(self, user_id: int, digest_key: str) -> None:
        await self.db.execute(
            """
            INSERT OR IGNORE INTO digest_runs (user_id, digest_key, sent_at)
            VALUES (?, ?, ?)
            """,
            (user_id, digest_key, utc_now()),
        )
        await self.db.commit()
