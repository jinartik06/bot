from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from src.ai import IdeaAI
from src.config import Config
from src.db import Database
from src.main import category_name_from_chat_text, delete_local_photo, merge_photo_text, parse_admin_user_input, safe_photo_name, split_category_hint


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
        self.assertEqual(payload["tasks"], [])
        self.assertEqual(payload["tags"], [])

    def test_fallback_entries_split_bulleted_message(self) -> None:
        ai = IdeaAI(make_config())

        entries = ai._fallback_entries(
            "- Надо подготовить wireframe главного экрана\n"
            "- Идея сделать поиск по всем заметкам\n"
            "- Купить микрофон для голосовых тестов",
            "text",
        )

        self.assertEqual(len(entries), 3)
        self.assertEqual(entries[0]["type"], "Задача")
        self.assertEqual(entries[2]["type"], "Покупка")

    def test_parse_admin_user_input(self) -> None:
        self.assertEqual(parse_admin_user_input("123456 спам"), (123456, "спам"))
        self.assertEqual(parse_admin_user_input("123456"), (123456, None))

    def test_photo_text_merges_caption_and_ocr(self) -> None:
        self.assertEqual(
            merge_photo_text("идея по экрану", "Мысли и поиск"),
            "идея по экрану\n\nТекст с фото: Мысли и поиск",
        )
        self.assertEqual(merge_photo_text("", "Мысли и поиск"), "Фото.\n\nТекст с фото: Мысли и поиск")
        self.assertEqual(merge_photo_text("", None), "Фото без подписи.")

    def test_safe_photo_name_sanitizes_unique_id_and_extension(self) -> None:
        self.assertEqual(
            safe_photo_name(10, "abc/../x", ".png"),
            Path("10") / "abc_x.png",
        )
        self.assertEqual(
            safe_photo_name(10, "abc", ".exe"),
            Path("10") / "abc.jpg",
        )

    def test_delete_local_photo_only_inside_storage_dir(self) -> None:
        with TemporaryDirectory() as tmpdir:
            base = Path(tmpdir) / "photos"
            base.mkdir()
            inside = base / "photo.jpg"
            outside = Path(tmpdir) / "outside.jpg"
            inside.write_bytes(b"photo")
            outside.write_bytes(b"photo")
            config = make_config()
            object.__setattr__(config, "photo_storage_dir", base)

            self.assertTrue(delete_local_photo(str(inside), config))
            self.assertFalse(inside.exists())
            self.assertFalse(delete_local_photo(str(outside), config))
            self.assertTrue(outside.exists())


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

    async def test_archive_hides_idea_from_list_and_search(self) -> None:
        with TemporaryDirectory() as tmpdir:
            db = Database(Path(tmpdir) / "ideas.db")
            await db.connect()
            try:
                await db.upsert_user(10, "user", "User", "Europe/Moscow", 6, "19:00")
                idea_id = await db.create_idea(
                    10,
                    {
                        "type": "Задача",
                        "title": "Проверить мысли",
                        "summary": "Проверить, что архив скрывает запись.",
                        "tasks": ["Проверить архив"],
                        "next_step": "Открыть архив",
                        "category": "Работа",
                        "key_points": [],
                        "open_questions": [],
                        "tags": [],
                    },
                    "Проверить мысли",
                    "text",
                    None,
                )
                await db.archive_idea(10, idea_id)
                ideas = await db.list_ideas(10)
                search = await db.search_ideas(10, "мысли")
                archived = await db.archived_ideas(10)
                steps = await db.next_step_ideas(10)
            finally:
                await db.close()

        self.assertEqual(ideas, [])
        self.assertEqual(search, [])
        self.assertEqual(steps, [])
        self.assertEqual(len(archived), 1)
        self.assertIsNotNone(archived[0]["archived_at"])

    async def test_album_lists_and_removes_uploaded_photo(self) -> None:
        with TemporaryDirectory() as tmpdir:
            db = Database(Path(tmpdir) / "ideas.db")
            await db.connect()
            try:
                await db.upsert_user(10, "user", "User", "Europe/Moscow", 6, "19:00")
                idea_id = await db.create_idea(
                    10,
                    {
                        "type": "Идея",
                        "title": "Фото доски",
                        "summary": "На фото схема раздела мыслей.",
                        "tasks": [],
                        "category": "Идеи",
                        "key_points": [],
                        "open_questions": [],
                        "tags": [],
                    },
                    "Фото доски",
                    "photo",
                    "telegram-file-id",
                    "data/photos/10/photo.jpg",
                    "Мысли, поиск, архив",
                )
                album_before = await db.album_photos(10)
                removed = await db.remove_idea_photo(10, idea_id)
                album_after = await db.album_photos(10)
                row = await db.get_idea(10, idea_id)
            finally:
                await db.close()

        self.assertEqual(len(album_before), 1)
        self.assertEqual(album_before[0]["photo_ocr_text"], "Мысли, поиск, архив")
        self.assertIsNotNone(removed)
        self.assertEqual(album_after, [])
        self.assertIsNone(row["photo_file_id"])
        self.assertIsNone(row["photo_path"])
        self.assertIsNone(row["photo_ocr_text"])


if __name__ == "__main__":
    unittest.main()
