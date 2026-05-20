from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from src.ai import IdeaAI
from src.config import Config


def make_config(
    *,
    groq_api_key: str | None = None,
    voice_transcriber: str = "faster_whisper",
) -> Config:
    return Config(
        bot_token="test-token",
        groq_api_key=groq_api_key,
        groq_base_url="https://api.groq.com",
        admin_ids=set(),
        allowed_ids=set(),
        allow_all_users=True,
        database_path=Path("data/ideas.db"),
        default_timezone="Europe/Moscow",
        default_digest_weekday=6,
        default_digest_time="19:00",
        groq_text_model="qwen/qwen3-32b",
        groq_vision_model="meta-llama/llama-4-scout-17b-16e-instruct",
        groq_transcribe_model="whisper-large-v3-turbo",
        voice_transcriber=voice_transcriber,
        short_input_char_limit=900,
    )


class VoiceHelperTests(unittest.TestCase):
    def test_local_whisper_transcriber_is_default_voice_engine(self) -> None:
        ai = IdeaAI(make_config())

        self.assertTrue(ai.uses_local_whisper_transcriber())
        self.assertEqual(ai.can_transcribe(), ai.local_whisper_runtime_issue() is None)

    def test_groq_voice_transcriber_requires_key(self) -> None:
        without_key = IdeaAI(make_config(voice_transcriber="groq"))
        with_key = IdeaAI(make_config(groq_api_key="test-key", voice_transcriber="groq"))

        self.assertTrue(without_key.uses_groq_transcriber())
        self.assertFalse(without_key.can_transcribe())
        self.assertTrue(with_key.can_transcribe())

    def test_unknown_voice_transcriber_is_disabled(self) -> None:
        ai = IdeaAI(make_config(voice_transcriber="unknown"))

        self.assertFalse(ai.can_transcribe())

    def test_local_whisper_runtime_issue_blocks_native_load(self) -> None:
        ai = IdeaAI(make_config())

        with patch.object(ai, "local_whisper_runtime_issue", return_value="unsupported runtime"):
            with self.assertRaisesRegex(RuntimeError, "unsupported runtime"):
                asyncio.run(ai.transcribe(Path("voice.ogg")))

    def test_clean_transcript_removes_filler_words(self) -> None:
        ai = IdeaAI(make_config())

        self.assertEqual(
            ai.clean_transcript("эээ ну идея сделать голосовой ввод, короче, быстрее"),
            "идея сделать голосовой ввод, быстрее",
        )


class VoiceAudioPreparationTests(unittest.IsolatedAsyncioTestCase):
    async def test_whisper_audio_conversion_reports_missing_ffmpeg(self) -> None:
        ai = IdeaAI(make_config())

        with (
            patch("src.ai.asyncio.create_subprocess_exec", new=AsyncMock(side_effect=FileNotFoundError())),
            patch.object(ai, "_bundled_ffmpeg_binary", return_value=None),
        ):
            with self.assertRaisesRegex(RuntimeError, "ffmpeg is not installed"):
                await ai._convert_to_whisper_wav(Path("voice.ogg"), Path("voice.wav"))

    async def test_whisper_audio_conversion_falls_back_to_bundled_ffmpeg(self) -> None:
        ai = IdeaAI(make_config())
        executables: list[str] = []

        async def fake_run(args: list[str], timeout_message: str) -> bytes:
            executables.append(args[0])
            if len(executables) == 1:
                raise FileNotFoundError()
            return b"ok"

        with (
            patch.object(ai, "_run_ffmpeg_command", side_effect=fake_run),
            patch.object(ai, "_bundled_ffmpeg_binary", return_value="bundled-ffmpeg"),
            patch.object(Path, "exists", return_value=True),
            patch.object(Path, "stat") as stat_mock,
        ):
            stat_mock.return_value.st_size = 100
            await ai._convert_to_whisper_wav(Path("voice.ogg"), Path("voice.wav"))

        self.assertEqual(executables, ["ffmpeg", "bundled-ffmpeg"])

    async def test_local_whisper_failure_does_not_fall_back_to_groq(self) -> None:
        ai = IdeaAI(make_config(groq_api_key="test-key", voice_transcriber="faster_whisper"))

        with (
            patch.object(ai, "local_whisper_runtime_issue", return_value=None),
            patch.object(ai, "_transcribe_faster_whisper", new=AsyncMock(side_effect=RuntimeError("whisper down"))),
        ):
            with self.assertRaisesRegex(RuntimeError, "whisper down"):
                await ai.transcribe(Path("voice.ogg"))

    async def test_groq_voice_transcriber_uses_separate_service(self) -> None:
        ai = IdeaAI(make_config(groq_api_key="test-key", voice_transcriber="groq"))

        with patch.object(ai._groq_voice, "transcribe", new=AsyncMock(return_value="эээ ну тестовая расшифровка")) as transcribe:
            result = await ai.transcribe(Path("voice.ogg"))

        transcribe.assert_awaited_once_with(Path("voice.ogg"))
        self.assertEqual(result, "тестовая расшифровка")

    async def test_groq_voice_empty_response_is_reported(self) -> None:
        ai = IdeaAI(make_config(groq_api_key="test-key", voice_transcriber="groq"))

        with patch.object(ai._groq_voice, "transcribe", new=AsyncMock(return_value="")):
            with self.assertRaisesRegex(RuntimeError, "empty transcript"):
                await ai.transcribe(Path("voice.ogg"))


if __name__ == "__main__":
    unittest.main()
