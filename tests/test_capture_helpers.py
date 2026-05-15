from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from src.ai import IdeaAI
from src.config import Config
from src.db import Database
from src.main import category_name_from_chat_text, parse_admin_user_input, split_category_hint


def make_config() -> Config:
    return Config(
        bot_token="test-token",
        groq_api_key=None,
        groq_base_url="https://api.groq.com",
        admin_ids=set(),
        allowed_ids=set(),
        allow_all_users=True,
        database_path=Path("data/ideas.db"),
        default_timezone="Europe/Moscow",
        default_digest_weekday=6,
        default_digest_time="19:00",
        groq_text_model="qwen/qwen3-32b",
        groq_transcribe_model="whisper-large-v3-turbo",
        voice_transcriber="faster_whisper",
        short_input_char_limit=900,
    )


class CaptureHelperTests(unittest.TestCase):
    def test_split_category_hint_from_trailing_marker(self) -> None:
        body, category = split_category_hint("Сделать калькулятор кирпича категория: маркетинг")

        self.assertEqual(body, "Сделать калькулятор кирпича")
        self.assertEqual(category, "маркетинг")

    def test_split_category_hint_from_trailing_marker_without_colon(self) -> None:
        body, category = split_category_hint("Сделать калькулятор кирпича категория маркетинг")

        self.assertEqual(body, "Сделать калькулятор кирпича")
        self.assertEqual(category, "маркетинг")

    def test_split_category_hint_for_last_idea_command(self) -> None:
        body, category = split_category_hint("категория продажи")

        self.assertEqual(body, "")
        self.assertEqual(category, "продажи")

    def test_category_name_from_chat_text_removes_command_words(self) -> None:
        self.assertEqual(category_name_from_chat_text("категория: продажи"), "продажи")
        self.assertEqual(category_name_from_chat_text("в категорию маркетинг"), "маркетинг")

    def test_raw_idea_payload_does_not_add_analysis(self) -> None:
        ai = IdeaAI(make_config())

        payload = ai.raw_idea_payload("Сырая мысль без разбора", "продукт")

        self.assertEqual(payload["title"], "Сырая мысль без разбора")
        self.assertEqual(payload["category"], "продукт")
        self.assertIsNone(payload["summary"])
        self.assertEqual(payload["key_points"], [])
        self.assertEqual(payload["tags"], [])

    def test_parse_admin_user_input(self) -> None:
        self.assertEqual(parse_admin_user_input("123456 спам"), (123456, "спам"))
        self.assertEqual(parse_admin_user_input("123456"), (123456, None))


class ProcessedMessageTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_message_is_processed_once(self) -> None:
        with TemporaryDirectory() as tmpdir:
            db = Database(Path(tmpdir) / "ideas.db")
            await db.connect()
            try:
                first = await db.mark_message_processed(10, 20, 30)
                second = await db.mark_message_processed(10, 20, 30)
            finally:
                await db.close()

        self.assertTrue(first)
        self.assertFalse(second)

    async def test_runtime_lock_blocks_other_owner_until_release_or_expiry(self) -> None:
        with TemporaryDirectory() as tmpdir:
            db = Database(Path(tmpdir) / "ideas.db")
            await db.connect()
            try:
                first = await db.try_acquire_runtime_lock("voice", "owner-a", 60)
                second = await db.try_acquire_runtime_lock("voice", "owner-b", 60)
                same_owner_refresh = await db.try_acquire_runtime_lock("voice", "owner-a", 60)
                wrong_release = await db.release_runtime_lock("voice", "owner-b")
                released = await db.release_runtime_lock("voice", "owner-a")
                after_release = await db.try_acquire_runtime_lock("voice", "owner-b", 60)
                await db.db.execute("UPDATE runtime_locks SET expires_at = 0 WHERE name = ?", ("voice",))
                await db.db.commit()
                after_expiry = await db.try_acquire_runtime_lock("voice", "owner-c", 60)
            finally:
                await db.close()

        self.assertTrue(first)
        self.assertFalse(second)
        self.assertTrue(same_owner_refresh)
        self.assertFalse(wrong_release)
        self.assertTrue(released)
        self.assertTrue(after_release)
        self.assertTrue(after_expiry)

    async def test_admin_user_list_includes_blocked_and_allowed_users(self) -> None:
        with TemporaryDirectory() as tmpdir:
            db = Database(Path(tmpdir) / "ideas.db")
            await db.connect()
            try:
                await db.seed_allowed({1, 3}, {1})
                await db.upsert_user(2, "user2", "User Two", "Europe/Moscow", 6, "19:00")
                await db.block_user(2, "user2", "spam", 1)
                await db.block_user(4, None, None, 1)

                self.assertTrue(await db.is_blocked(2))
                rows = await db.list_admin_users()
                by_id = {row["telegram_id"]: row for row in rows}
                blocked_rows = await db.list_blocked()
            finally:
                await db.close()

        self.assertTrue(by_id[1]["is_admin"])
        self.assertTrue(by_id[3]["is_allowed"])
        self.assertTrue(by_id[2]["is_blocked"])
        self.assertEqual(by_id[2]["block_reason"], "spam")
        self.assertIn(4, by_id)
        self.assertEqual({row["telegram_id"] for row in blocked_rows}, {2, 4})

    async def test_pin_idea_marks_chat_message(self) -> None:
        with TemporaryDirectory() as tmpdir:
            db = Database(Path(tmpdir) / "ideas.db")
            await db.connect()
            try:
                await db.upsert_user(10, "user", "User", "Europe/Moscow", 6, "19:00")
                idea_id = await db.create_idea(
                    10,
                    {
                        "title": "Идея",
                        "category": "Продукт",
                        "key_points": [],
                        "open_questions": [],
                        "tags": [],
                    },
                    "Текст идеи",
                    "text",
                    None,
                )
                await db.pin_idea(10, idea_id, 1000, 2000)
                row = await db.get_idea(10, idea_id)
            finally:
                await db.close()

        self.assertIsNotNone(row["pinned_at"])
        self.assertEqual(row["pinned_chat_id"], 1000)
        self.assertEqual(row["pinned_message_id"], 2000)


if __name__ == "__main__":
    unittest.main()
