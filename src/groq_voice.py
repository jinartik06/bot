from __future__ import annotations

import json
import logging
import mimetypes
from pathlib import Path

import aiohttp

from .config import Config


logger = logging.getLogger("ideas_bot.groq_voice")


class GroqVoiceTranscriber:
    def __init__(self, config: Config):
        self.config = config

    @property
    def is_configured(self) -> bool:
        return bool(self.config.groq_api_key)

    @property
    def audio_url(self) -> str:
        return f"{self.config.groq_base_url.rstrip('/')}/openai/v1/audio/transcriptions"

    @property
    def models_url(self) -> str:
        return f"{self.config.groq_base_url.rstrip('/')}/openai/v1/models"

    async def check_startup(self) -> tuple[bool, str]:
        if not self.config.groq_api_key:
            return False, "GROQ_API_KEY is required for Groq voice transcription"

        logger.info(
            "Groq voice startup check: base_url=%s audio_model=%s language=%s timeout=%s",
            self.config.groq_base_url,
            self.config.groq_transcribe_model,
            self.config.whisper_language or "auto",
            self.config.voice_processing_timeout_seconds,
        )

        headers = {"Authorization": f"Bearer {self.config.groq_api_key}"}
        timeout = aiohttp.ClientTimeout(total=min(20, max(5, self.config.voice_processing_timeout_seconds)))
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(self.models_url, headers=headers) as response:
                    text = await response.text()
                    if response.status >= 400:
                        detail = text[:500].replace("\n", " ")
                        logger.error("Groq voice startup check failed: http=%s body=%s", response.status, detail)
                        return False, f"Groq /models failed with HTTP {response.status}: {detail}"
        except Exception as exc:
            logger.exception("Groq voice startup check crashed")
            return False, f"Groq startup check error: {exc}"

        return True, f"Groq voice ready: model={self.config.groq_transcribe_model}"

    async def transcribe(self, path: Path) -> str:
        if not self.config.groq_api_key:
            raise RuntimeError("GROQ_API_KEY is required for Groq voice transcription")
        if not path.exists():
            raise RuntimeError(f"Audio file does not exist: {path}")
        if path.stat().st_size == 0:
            raise RuntimeError(f"Audio file is empty: {path}")

        mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        form = aiohttp.FormData()
        form.add_field("model", self.config.groq_transcribe_model)
        if self.config.whisper_language:
            form.add_field("language", self.config.whisper_language)
        form.add_field("response_format", "json")
        form.add_field("file", path.read_bytes(), filename=path.name, content_type=mime_type)

        logger.info(
            "Groq audio transcription request: model=%s suffix=%s bytes=%s mime=%s language=%s",
            self.config.groq_transcribe_model,
            path.suffix or "unknown",
            path.stat().st_size,
            mime_type,
            self.config.whisper_language or "auto",
        )

        headers = {"Authorization": f"Bearer {self.config.groq_api_key}"}
        timeout = aiohttp.ClientTimeout(total=self.config.voice_processing_timeout_seconds)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(self.audio_url, headers=headers, data=form) as response:
                text = await response.text()
                if response.status >= 400:
                    detail = text[:500].replace("\n", " ")
                    logger.error("Groq audio transcription failed: http=%s body=%s", response.status, detail)
                    raise RuntimeError(f"Groq audio transcription failed with HTTP {response.status}: {detail}")
                try:
                    data = json.loads(text)
                except json.JSONDecodeError as exc:
                    logger.error("Groq audio transcription returned non-JSON response: %s", text[:500])
                    raise RuntimeError("Groq audio transcription returned non-JSON response") from exc

        transcript = str(data.get("text") or "").strip()
        logger.info("Groq audio transcription finished: chars=%s", len(transcript))
        return transcript
