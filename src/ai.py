from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import aiohttp

from .config import Config
from .groq_voice import GroqVoiceTranscriber


logger = logging.getLogger("ideas_bot.ai")
IMAGE_MIME_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}


class EmptyTranscriptError(RuntimeError):
    pass


class IdeaAI:
    FILLER_WORDS = {
        "а",
        "ах",
        "вот",
        "как бы",
        "короче",
        "м",
        "мда",
        "мм",
        "ммм",
        "ну",
        "ну вот",
        "ну короче",
        "ну типа",
        "типа",
        "угу",
        "ум",
        "хм",
        "хмм",
        "э",
        "ээ",
        "эээ",
        "эм",
        "эмм",
        "эммм",
    }
    ENTRY_TYPES = {
        "Идея",
        "Задача",
        "Напоминание",
        "Контент",
        "Покупка",
        "Мысль",
        "Инсайт",
        "Ссылка",
        "Наблюдение",
    }
    DEFAULT_CATEGORIES = {
        "Работа",
        "Бизнес",
        "Контент",
        "Личное",
        "Здоровье",
        "Финансы",
        "Обучение",
        "Покупки",
        "Идеи",
        "Без категории",
    }

    def __init__(self, config: Config):
        self.config = config
        self._voice_lock = asyncio.Lock()
        self._whisper_model: Any | None = None
        self._groq_voice = GroqVoiceTranscriber(config)

    def has_api(self) -> bool:
        return bool(self.config.groq_api_key)

    def uses_groq_transcriber(self) -> bool:
        return self.config.voice_transcriber in {"groq", "groq_whisper", "groq_audio"}

    def uses_local_whisper_transcriber(self) -> bool:
        return self.config.voice_transcriber in {"faster_whisper", "local_whisper", "whisper"}

    def local_whisper_runtime_issue(self) -> str | None:
        if os.name == "nt" and sys.version_info >= (3, 14):
            return "Python 3.14 on Windows is not supported by faster-whisper native runtime; use Python 3.12 or Docker"
        return None

    def can_transcribe(self) -> bool:
        if self.uses_local_whisper_transcriber():
            return self.local_whisper_runtime_issue() is None
        if self.uses_groq_transcriber():
            return bool(self.config.groq_api_key)
        return False

    def voice_is_busy(self) -> bool:
        return self._voice_lock.locked()

    async def check_voice_transcriber(self) -> tuple[bool, str]:
        if self.uses_local_whisper_transcriber():
            runtime_issue = self.local_whisper_runtime_issue()
            if runtime_issue:
                return False, runtime_issue
            try:
                import faster_whisper  # noqa: F401
            except ImportError:
                return False, "faster-whisper is not installed"
            return True, f"faster-whisper model={self.config.whisper_model} will load on first voice"
        if self.uses_groq_transcriber():
            return await self._groq_voice.check_startup()
        return False, f"Unsupported VOICE_TRANSCRIBER={self.config.voice_transcriber}"

    async def transcribe(self, path: Path) -> str:
        if self.uses_local_whisper_transcriber():
            runtime_issue = self.local_whisper_runtime_issue()
            if runtime_issue:
                raise RuntimeError(runtime_issue)
            async with self._voice_lock:
                return await self._transcribe_faster_whisper(path)
        if self.uses_groq_transcriber():
            return await self._transcribe_groq_audio(path)
        raise RuntimeError(f"Unsupported VOICE_TRANSCRIBER: {self.config.voice_transcriber}")

    async def _transcribe_groq_audio(self, path: Path) -> str:
        transcript = self.clean_transcript(await self._groq_voice.transcribe(path))
        if not transcript:
            raise EmptyTranscriptError("Groq audio transcription returned an empty transcript")
        return transcript

    async def _transcribe_faster_whisper(self, path: Path) -> str:
        with tempfile.TemporaryDirectory() as tmpdir:
            wav_path = Path(tmpdir) / "voice_input.wav"
            await self._convert_to_whisper_wav(path, wav_path)
            transcript = await asyncio.to_thread(self._transcribe_whisper_file, wav_path)
        transcript = self.clean_transcript(transcript)
        if not transcript:
            raise EmptyTranscriptError("Local Whisper returned an empty transcript")
        return transcript

    async def _convert_to_whisper_wav(self, source: Path, wav_path: Path) -> None:
        args = [
            self.config.ffmpeg_binary or "ffmpeg",
            "-y",
            "-i",
            str(source),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-f",
            "wav",
            str(wav_path),
        ]
        try:
            await self._run_ffmpeg_command(args, "ffmpeg timed out while preparing audio for Whisper")
        except FileNotFoundError as exc:
            fallback = self._bundled_ffmpeg_binary()
            if not fallback or fallback == args[0]:
                raise RuntimeError(
                    "ffmpeg is not installed or not available in PATH. "
                    "Install ffmpeg or reinstall Python dependencies so imageio-ffmpeg is available."
                ) from exc
            args[0] = fallback
            await self._run_ffmpeg_command(args, "ffmpeg timed out while preparing audio for Whisper")
        if not wav_path.exists() or wav_path.stat().st_size == 0:
            raise RuntimeError("ffmpeg did not create WAV output for Whisper")
        logger.info("Audio prepared for Whisper: source=%s wav_bytes=%s", source.suffix or "unknown", wav_path.stat().st_size)

    def _transcribe_whisper_file(self, wav_path: Path) -> str:
        model = self._get_whisper_model()
        segments, info = model.transcribe(
            str(wav_path),
            language=self.config.whisper_language or None,
            beam_size=self.config.whisper_beam_size,
            vad_filter=self.config.whisper_vad_filter,
        )
        text = " ".join(segment.text.strip() for segment in segments if segment.text and segment.text.strip())
        logger.info(
            "Whisper transcription finished: language=%s probability=%.3f duration=%.2f chars=%s",
            getattr(info, "language", None),
            float(getattr(info, "language_probability", 0.0) or 0.0),
            float(getattr(info, "duration", 0.0) or 0.0),
            len(text),
        )
        return text

    def _get_whisper_model(self) -> Any:
        if self._whisper_model is not None:
            return self._whisper_model
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise RuntimeError("faster-whisper is not installed") from exc

        cache_dir = self.config.whisper_model_cache_dir
        cache_dir.mkdir(parents=True, exist_ok=True)
        logger.info(
            "Loading faster-whisper model: model=%s device=%s compute_type=%s cache=%s",
            self.config.whisper_model,
            self.config.whisper_device,
            self.config.whisper_compute_type,
            cache_dir,
        )
        self._whisper_model = WhisperModel(
            self.config.whisper_model,
            device=self.config.whisper_device,
            compute_type=self.config.whisper_compute_type,
            cpu_threads=self.config.whisper_cpu_threads,
            num_workers=self.config.whisper_num_workers,
            download_root=str(cache_dir),
        )
        return self._whisper_model

    def clean_transcript(self, text: str) -> str:
        normalized = " ".join(text.replace("ё", "е").split())
        if not normalized:
            return ""

        tokens = re.findall(r"[A-Za-zА-Яа-яЁё]+|\d+|[^\w\s]", normalized, flags=re.UNICODE)
        cleaned: list[str] = []
        index = 0
        while index < len(tokens):
            token = tokens[index]
            lower = token.lower()
            next_lower = tokens[index + 1].lower() if index + 1 < len(tokens) else ""
            two_words = f"{lower} {next_lower}" if next_lower else ""

            if re.fullmatch(r"[,.!?;:…-]+", token):
                if cleaned and not re.fullmatch(r"[,.!?;:…-]+", cleaned[-1]):
                    cleaned.append(token)
                index += 1
                continue

            if two_words in self.FILLER_WORDS:
                index += 2
                continue
            if lower in self.FILLER_WORDS or self._is_filler_sound(lower):
                index += 1
                continue

            cleaned.append(token)
            index += 1

        result = self._join_tokens(cleaned)
        result = re.sub(r"\s+", " ", result).strip(" ,.!?;:…-")
        return result

    def _is_filler_sound(self, value: str) -> bool:
        letters_only = re.sub(r"[^a-zа-яе]", "", value.lower())
        if len(letters_only) <= 1:
            return letters_only in {"а", "м", "э"}
        return bool(re.fullmatch(r"(э+|е+|а+|м+|у+|хм+|эм+|мм+)", letters_only))

    def _join_tokens(self, tokens: list[str]) -> str:
        result = ""
        no_space_before = set(",.!?;:…")
        no_space_after = {"("}
        for token in tokens:
            if not result:
                result = token
            elif token in no_space_before:
                result += token
            elif result[-1] in no_space_after:
                result += token
            else:
                result += f" {token}"
        return result

    async def _run_ffmpeg_command(self, args: list[str], timeout_message: str) -> bytes:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=self.config.media_command_timeout_seconds,
            )
        except asyncio.CancelledError:
            if process.returncode is None:
                process.kill()
            await process.wait()
            raise
        except asyncio.TimeoutError as exc:
            if process.returncode is None:
                process.kill()
            await process.wait()
            raise RuntimeError(timeout_message) from exc
        if process.returncode != 0:
            detail = stderr.decode("utf-8", errors="ignore")[-500:]
            raise RuntimeError(f"ffmpeg failed to prepare audio: {detail}")
        return stderr

    def _bundled_ffmpeg_binary(self) -> str | None:
        try:
            import imageio_ffmpeg
        except ImportError:
            return None
        try:
            return imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            logger.exception("imageio-ffmpeg is installed but did not return an ffmpeg binary")
            return None

    def raw_idea_payload(self, raw_text: str, category: str | None = None) -> dict[str, Any]:
        clean = raw_text.strip()
        first_line = next((line.strip() for line in clean.splitlines() if line.strip()), clean[:80])
        title = first_line[:80] or "Новая мысль"
        return {
            "type": self._infer_type(clean, "text"),
            "title": title,
            "summary": None,
            "tldr": None,
            "full_text": None,
            "key_points": [],
            "tasks": [],
            "open_questions": [],
            "next_step": None,
            "side_thoughts": None,
            "category": category or "Без категории",
            "tags": [],
            "original_text": clean,
        }

    def photo_without_text_payload(self, raw_text: str = "Фото без подписи, AI-описания и распознанного текста.") -> dict[str, Any]:
        clean = raw_text.strip() or "Фото без подписи, AI-описания и распознанного текста."
        return {
            "type": "Наблюдение",
            "title": "Фото без подписи",
            "summary": "Фото сохранено в альбом. Подписи, AI-описания или распознанного текста нет, поэтому я не придумываю содержание изображения.",
            "tldr": None,
            "full_text": None,
            "key_points": [],
            "tasks": [],
            "open_questions": [],
            "next_step": None,
            "side_thoughts": None,
            "category": "Личное",
            "tags": ["фото", "альбом"],
            "original_text": clean,
        }

    async def structure_entries(
        self,
        raw_text: str,
        source_type: str,
        has_photo: bool,
        *,
        allow_fallback: bool = True,
    ) -> list[dict[str, Any]]:
        clean = raw_text.strip()
        if not clean:
            return []
        if not self.has_api():
            if not allow_fallback:
                raise RuntimeError("GROQ_API_KEY is not configured")
            return self._fallback_entries(clean, source_type)

        size = "short" if len(clean) <= self.config.short_input_char_limit else "long"
        prompt = self._entries_prompt(clean, source_type, has_photo, size)
        try:
            content = await self._create_text_response(prompt)
        except Exception:
            logger.exception("Groq text structuring failed, using fallback cards")
            if not allow_fallback:
                raise
            return self._fallback_entries(clean, source_type)

        data = self._parse_json_response(content)
        if data is None:
            logger.warning("Groq returned non-JSON response, using fallback cards")
            if not allow_fallback:
                raise RuntimeError("Groq returned non-JSON response")
            return self._fallback_entries(clean, source_type)

        cards = data.get("cards") or data.get("entries") or data.get("items")
        if not isinstance(cards, list):
            cards = [data]
        normalized = [
            self._normalize(card, clean)
            for card in cards[:12]
            if isinstance(card, dict)
        ]
        return normalized or self._fallback_entries(clean, source_type)

    async def structure_idea(
        self,
        raw_text: str,
        source_type: str,
        has_photo: bool,
        *,
        allow_fallback: bool = True,
    ) -> dict[str, Any]:
        clean = raw_text.strip()
        if not self.has_api():
            if not allow_fallback:
                raise RuntimeError("GROQ_API_KEY is not configured")
            return self._fallback(clean)

        size = "short" if len(clean) <= self.config.short_input_char_limit else "long"
        prompt = self._prompt(clean, source_type, has_photo, size)
        try:
            content = await self._create_text_response(prompt)
        except Exception:
            logger.exception("Groq text structuring failed, using fallback structure")
            if not allow_fallback:
                raise
            return self._fallback(clean)
        data = self._parse_json_response(content)
        if data is None:
            logger.warning("Groq returned non-JSON response, using fallback structure")
            if not allow_fallback:
                raise RuntimeError("Groq returned non-JSON response")
            return self._fallback(clean)
        return self._normalize(data, clean)

    async def _create_text_response(self, prompt: str) -> str:
        system_prompt = (
            "You are an editor for a simple personal place for thoughts, tasks, ideas and links. "
            "Preserve the user's meaning and important details. "
            "Write user-facing values in Russian. Return only one valid JSON object."
        )
        payload: dict[str, Any] = {
            "model": self.config.groq_text_model,
            "input": f"{system_prompt}\n\n{prompt}",
            "temperature": 0.2,
        }
        headers = {
            "Authorization": f"Bearer {self.config.groq_api_key}",
            "Content-Type": "application/json",
        }
        url = f"{self.config.groq_base_url.rstrip('/')}/openai/v1/responses"
        timeout = aiohttp.ClientTimeout(total=60)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, headers=headers, json=payload) as response:
                text = await response.text()
                if response.status >= 400:
                    raise RuntimeError(f"Groq Responses API failed with HTTP {response.status}: {text[:500]}")
                try:
                    data = json.loads(text)
                except json.JSONDecodeError as exc:
                    raise RuntimeError("Groq Responses API returned non-JSON response") from exc
        return self._extract_response_text(data)

    async def describe_photo(self, path: Path, caption: str = "", ocr_text: str | None = None) -> str | None:
        if not self.config.photo_vision_enabled or not self.has_api():
            return None
        if not path.exists() or not path.is_file():
            return None

        suffix = path.suffix.lower()
        mime_type = IMAGE_MIME_TYPES.get(suffix, "image/jpeg")
        try:
            image_bytes = await asyncio.to_thread(path.read_bytes)
            image_b64 = base64.b64encode(image_bytes).decode("ascii")
            content = await self._create_photo_response(
                self._photo_prompt(caption, ocr_text),
                f"data:{mime_type};base64,{image_b64}",
            )
        except Exception:
            logger.exception("Groq vision analysis failed, continuing without image description")
            return None

        clean = " ".join(content.split())
        return clean[:1200] or None

    async def _create_photo_response(self, prompt: str, image_url: str) -> str:
        try:
            return await self._create_photo_response_via_responses(prompt, image_url)
        except Exception as exc:
            logger.info("Groq Responses vision request failed, trying chat completions: %s", exc)
            return await self._create_photo_response_via_chat(prompt, image_url)

    async def _create_photo_response_via_responses(self, prompt: str, image_url: str) -> str:
        payload: dict[str, Any] = {
            "model": self.config.groq_vision_model,
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": prompt},
                        {"type": "input_image", "image_url": image_url},
                    ],
                }
            ],
            "temperature": 0.1,
        }
        headers = {
            "Authorization": f"Bearer {self.config.groq_api_key}",
            "Content-Type": "application/json",
        }
        url = f"{self.config.groq_base_url.rstrip('/')}/openai/v1/responses"
        timeout = aiohttp.ClientTimeout(total=max(5, self.config.photo_vision_timeout_seconds))
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, headers=headers, json=payload) as response:
                text = await response.text()
                if response.status >= 400:
                    raise RuntimeError(f"Groq vision Responses API failed with HTTP {response.status}: {text[:500]}")
                data = json.loads(text)
        return self._extract_response_text(data)

    async def _create_photo_response_via_chat(self, prompt: str, image_url: str) -> str:
        payload: dict[str, Any] = {
            "model": self.config.groq_vision_model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": image_url}},
                    ],
                }
            ],
            "temperature": 0.1,
            "max_tokens": 700,
        }
        headers = {
            "Authorization": f"Bearer {self.config.groq_api_key}",
            "Content-Type": "application/json",
        }
        url = f"{self.config.groq_base_url.rstrip('/')}/openai/v1/chat/completions"
        timeout = aiohttp.ClientTimeout(total=max(5, self.config.photo_vision_timeout_seconds))
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, headers=headers, json=payload) as response:
                text = await response.text()
                if response.status >= 400:
                    raise RuntimeError(f"Groq vision Chat API failed with HTTP {response.status}: {text[:500]}")
                data = json.loads(text)
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return content
        raise RuntimeError("Groq vision Chat API returned no content")

    def _photo_prompt(self, caption: str, ocr_text: str | None) -> str:
        caption = caption.strip() or "нет"
        ocr_text = " ".join(str(ocr_text or "").split()) or "нет"
        return f"""
Ты разбираешь фото для личного inbox мыслей.

Нужно:
- коротко описать, что реально видно на изображении;
- отдельно упомянуть важные объекты, экран, документ, чек, вывеску, схему или интерфейс, если они видны;
- переписать видимый текст с фото, если он читается;
- учитывать подпись и OCR ниже, но не дублировать одно и то же длинно;
- если в чём-то не уверен, так и напиши: "похоже".

Не придумывай людей, места, бренды, даты, суммы и задачи, если они не видны на фото и не указаны в подписи/OCR.
Ответь по-русски обычным текстом, максимум 6 коротких пунктов, без JSON и markdown.

Подпись пользователя: {caption}
OCR Tesseract: {ocr_text}
"""

    def _extract_response_text(self, data: dict[str, Any]) -> str:
        output_text = data.get("output_text")
        if isinstance(output_text, str) and output_text.strip():
            return output_text

        parts: list[str] = []
        for item in data.get("output") or []:
            if not isinstance(item, dict) or item.get("type") != "message":
                continue
            for content in item.get("content") or []:
                if not isinstance(content, dict):
                    continue
                if content.get("type") in {"output_text", "text"} and isinstance(content.get("text"), str):
                    parts.append(content["text"])
        if parts:
            return "\n".join(parts).strip()
        raise RuntimeError("Groq Responses API returned no output text")

    def _entries_prompt(self, text: str, source_type: str, has_photo: bool, size: str) -> str:
        split_rule = (
            "If the message contains several independent thoughts, tasks, purchases, links or content ideas, "
            "split them into separate cards. If it is one coherent thought, return one card."
        )
        if size == "long":
            split_rule += " Prefer 3-10 cards for long chaotic notes, but do not split one idea just to fill a quota."

        return f"""
Source: {source_type}
Photo as context: {"yes" if has_photo else "no"}

Product behavior:
The bot is a simple place for thoughts. The user should not sort anything manually.

Photo rule:
If Source is photo, analyze only the caption, AI image description and OCR text provided in Original text.
Visual details are allowed only when they appear in "AI-описание фото"; text from the image is allowed only when it appears in OCR or the AI image description.
Do not invent objects, people, colors, layout, handwriting, documents, products or scene details beyond those inputs.
If Original text says "Фото без подписи, AI-описания и распознанного текста.", return exactly one simple card: type "Наблюдение", title "Фото без подписи", summary "Фото сохранено в альбом. Подписи, AI-описания или распознанного текста нет, поэтому я не придумываю содержание изображения.", tasks [], next_step null.

Task:
1. Clean filler sounds and filler words, but preserve meaning and useful details.
2. {split_rule}
3. For each card choose one type from: Идея, Задача, Напоминание, Контент, Покупка, Мысль, Инсайт, Ссылка, Наблюдение.
4. Choose a short category. Prefer: Работа, Бизнес, Контент, Личное, Здоровье, Финансы, Обучение, Покупки, Идеи.
5. Extract concrete tasks/action items into tasks.
6. Put the clearest next action into next_step. If there is no action, use null.
7. Keep each summary to 1-3 short sentences.
8. Return exactly one JSON object:
{{
  "cards": [
    {{
      "type": "Идея",
      "title": "short clear title in Russian",
      "summary": "1-3 short sentences in Russian",
      "full_text": "cleaned relevant full text for this card",
      "key_points": ["important details"],
      "tasks": ["task/action item"],
      "open_questions": ["uncertainty or question"],
      "next_step": "next action or null",
      "side_thoughts": "short side thought or null",
      "category": "short category",
      "tags": ["3-5 tags without #"],
      "original_text": "the exact relevant part of the user's message"
    }}
  ]
}}
Do not add markdown, code fences, explanations, or text around JSON.

Original text:
{text}
"""

    def _prompt(self, text: str, source_type: str, has_photo: bool, size: str) -> str:
        if size == "short":
            shape = """
For a short idea fill:
- type: one of Идея, Задача, Напоминание, Контент, Покупка, Мысль, Инсайт, Ссылка, Наблюдение
- title: one line, in Russian
- summary: 2-3 sentences, in Russian
- tldr: null
- full_text: null
- key_points: []
- tasks: task/action items as a list in Russian
- open_questions: []
- next_step: action item in Russian, or null
- side_thoughts: side thoughts in Russian, or null
- category: short category name in Russian
- tags: 3-5 Russian tags without #
"""
        else:
            shape = """
For a long detailed idea fill:
- type: one of Идея, Задача, Напоминание, Контент, Покупка, Мысль, Инсайт, Ссылка, Наблюдение
- title: one line, in Russian
- summary: null
- tldr: 2-3 sentences in Russian
- full_text: structured full transcript in Russian with headings and lists, do not drop important details
- key_points: key points as a list in Russian
- tasks: task/action items as a list in Russian
- open_questions: doubts, uncertainties, "need to think", questions, in Russian
- next_step: next step/action item in Russian, or null
- side_thoughts: separate side-thoughts block in Russian if present, otherwise null
- category: short category name in Russian
- tags: 3-5 Russian tags without #
"""
        return f"""
Source: {source_type}
Photo as context: {"yes" if has_photo else "no"}

Photo rule:
If Source is photo, analyze only the caption, AI image description and OCR text provided in Original text.
Do not describe visual details unless they are explicitly present in "AI-описание фото", OCR or caption.
If Original text says "Фото без подписи, AI-описания и распознанного текста.", return type "Наблюдение", title "Фото без подписи", no tasks and next_step null.

Task:
1. Remove filler sounds, hesitation noises and filler words such as "эээ", "ммм", "эм", "ну", "типа", "как бы", "короче"; preserve vivid meaningful phrasing.
2. Do not invent facts.
3. If the user was unsure, put that into open_questions.
4. If there are action items, put them into tasks and put the clearest one into next_step.
5. Return exactly these JSON keys:
type, title, summary, tldr, full_text, key_points, tasks, open_questions, next_step, side_thoughts, category, tags.
Do not add markdown, code fences, explanations, or text around JSON.

{shape}

Original text:
{text}
"""

    def _parse_json_response(self, content: str) -> dict[str, Any] | None:
        clean = content.strip()
        if not clean:
            return None
        if clean.startswith("```"):
            clean = re.sub(r"^```(?:json)?\s*", "", clean, flags=re.IGNORECASE)
            clean = re.sub(r"\s*```$", "", clean)
        try:
            data = json.loads(clean)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", clean, flags=re.DOTALL)
            if not match:
                return None
            try:
                data = json.loads(match.group(0))
            except json.JSONDecodeError:
                return None
        return data if isinstance(data, dict) else None

    def _fallback(self, text: str) -> dict[str, Any]:
        first_line = next((line.strip() for line in text.splitlines() if line.strip()), text[:80])
        title = first_line[:80] or "Новая идея"
        entry_type = self._infer_type(text, "text")
        tasks = self._infer_tasks(text)
        return {
            "type": entry_type,
            "title": title,
            "summary": self._short_summary(text),
            "tldr": None,
            "full_text": text if len(text) > 260 else None,
            "key_points": [],
            "tasks": tasks,
            "open_questions": [],
            "next_step": tasks[0] if tasks else None,
            "side_thoughts": None,
            "category": self._infer_category(text, entry_type),
            "tags": self._fallback_tags(text, entry_type),
            "original_text": text,
        }

    def _fallback_entries(self, text: str, source_type: str) -> list[dict[str, Any]]:
        parts = self._split_into_card_parts(text)
        return [self._normalize(self._fallback(part), part) for part in parts[:12]]

    def _normalize(self, data: dict[str, Any], text: str) -> dict[str, Any]:
        fallback = self._fallback(text)
        for key, value in fallback.items():
            data.setdefault(key, value)
        entry_type = str(data.get("type") or data.get("entry_type") or fallback["type"]).strip()
        data["type"] = entry_type if entry_type in self.ENTRY_TYPES else fallback["type"]
        for key in ("key_points", "tasks", "open_questions", "tags"):
            value = data.get(key)
            if isinstance(value, str):
                data[key] = [value]
            elif not isinstance(value, list):
                data[key] = []
            data[key] = [str(item).strip() for item in data[key] if str(item).strip()][:8]
        data["title"] = str(data.get("title") or fallback["title"]).strip()[:180] or fallback["title"]
        data["summary"] = self._nullable_text(data.get("summary"), 600) or fallback["summary"]
        data["tldr"] = self._nullable_text(data.get("tldr"), 700)
        data["full_text"] = self._nullable_text(data.get("full_text"), 4000)
        data["next_step"] = self._nullable_text(data.get("next_step"), 240)
        data["side_thoughts"] = self._nullable_text(data.get("side_thoughts"), 500)
        data["category"] = self._nullable_text(data.get("category"), 80) or fallback["category"]
        data["original_text"] = self._nullable_text(data.get("original_text"), 4000) or text
        data["tags"] = [str(tag).strip().lstrip("#")[:40] for tag in data["tags"] if str(tag).strip()][:5]
        if not data["tags"]:
            data["tags"] = fallback["tags"]
        return data

    def _nullable_text(self, value: Any, limit: int) -> str | None:
        if value is None:
            return None
        clean = " ".join(str(value).split())
        if not clean or clean.lower() == "null":
            return None
        return clean[:limit]

    def _short_summary(self, text: str) -> str:
        clean = " ".join(text.split())
        if len(clean) <= 260:
            return clean
        sentences = re.split(r"(?<=[.!?])\s+", clean)
        summary = ""
        for sentence in sentences:
            candidate = f"{summary} {sentence}".strip()
            if len(candidate) > 320:
                break
            summary = candidate
        return summary or clean[:260].rstrip(" ,.;:") + "..."

    def _split_into_card_parts(self, text: str) -> list[str]:
        clean = text.strip()
        bullet_matches = re.findall(r"(?m)^\s*(?:[-*•]|\d+[.)])\s+(.+)$", clean)
        if len(bullet_matches) >= 2 and len(clean) > 80:
            return [part.strip() for part in bullet_matches if len(part.strip()) > 8]

        paragraphs = [part.strip() for part in re.split(r"\n{2,}", clean) if part.strip()]
        if len(paragraphs) >= 2 and len(clean) > 600:
            return paragraphs

        if len(clean) <= 1800:
            return [clean]

        sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+", clean) if part.strip()]
        chunks: list[str] = []
        current = ""
        for sentence in sentences:
            candidate = f"{current} {sentence}".strip()
            if current and len(candidate) > 1200:
                chunks.append(current)
                current = sentence
            else:
                current = candidate
        if current:
            chunks.append(current)
        return chunks or [clean]

    def _infer_type(self, text: str, source_type: str) -> str:
        lower = text.lower()
        if source_type == "link" or re.search(r"https?://", lower):
            return "Ссылка"
        if re.search(r"\b(надо|нужно|сделать|проверить|написать|позвонить|запустить|подготовить)\b", lower):
            return "Задача"
        if re.search(r"\b(напомни|напоминание|не забыть|дедлайн)\b", lower):
            return "Напоминание"
        if re.search(r"\b(купить|заказать|покупка|цена|стоимость)\b", lower):
            return "Покупка"
        if re.search(r"\b(пост|ролик|сторис|контент|сценарий|видео)\b", lower):
            return "Контент"
        if re.search(r"\b(инсайт|понял|осознал|вывод)\b", lower):
            return "Инсайт"
        if re.search(r"\b(идея|можно сделать|придумал)\b", lower):
            return "Идея"
        if re.search(r"\b(заметил|наблюдение|вижу)\b", lower):
            return "Наблюдение"
        return "Мысль"

    def _infer_category(self, text: str, entry_type: str) -> str:
        lower = text.lower()
        if entry_type == "Покупка":
            return "Покупки"
        if re.search(r"\b(работа|клиент|проект|созвон)\b", lower):
            return "Работа"
        if re.search(r"\b(бизнес|продажи|деньги|выручка|маркетинг)\b", lower):
            return "Бизнес"
        if re.search(r"\b(пост|ролик|контент|канал|сторис)\b", lower):
            return "Контент"
        if re.search(r"\b(здоровье|спорт|сон|врач)\b", lower):
            return "Здоровье"
        if re.search(r"\b(финансы|счет|налог|бюджет)\b", lower):
            return "Финансы"
        if re.search(r"\b(курс|учеба|книга|обучение)\b", lower):
            return "Обучение"
        if entry_type == "Идея":
            return "Идеи"
        return "Личное"

    def _infer_tasks(self, text: str) -> list[str]:
        tasks: list[str] = []
        for line in re.split(r"[\n.;!?]+", text):
            clean = " ".join(line.split())
            if not clean:
                continue
            lower = clean.lower()
            if re.search(r"\b(надо|нужно|сделать|проверить|написать|позвонить|запустить|подготовить|купить|заказать)\b", lower):
                tasks.append(clean[:220])
        return tasks[:5]

    def _fallback_tags(self, text: str, entry_type: str) -> list[str]:
        tags = ["мысли", entry_type.lower()]
        category = self._infer_category(text, entry_type).lower()
        if category not in tags:
            tags.append(category)
        return tags[:5]
