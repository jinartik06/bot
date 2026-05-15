from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import aiohttp

from .config import Config


logger = logging.getLogger("ideas_bot.ai")


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

    def __init__(self, config: Config):
        self.config = config
        self._voice_lock = asyncio.Lock()
        self._whisper_model: Any | None = None

    def has_api(self) -> bool:
        return bool(self.config.groq_api_key)

    def uses_groq_transcriber(self) -> bool:
        return self.config.voice_transcriber in {"groq", "groq_whisper", "groq_audio"}

    def uses_local_whisper_transcriber(self) -> bool:
        return self.config.voice_transcriber in {"faster_whisper", "local_whisper", "whisper"}

    def can_transcribe(self) -> bool:
        if self.uses_local_whisper_transcriber():
            return True
        if self.uses_groq_transcriber():
            return bool(self.config.groq_api_key)
        return False

    def voice_is_busy(self) -> bool:
        return self._voice_lock.locked()

    async def check_voice_transcriber(self) -> tuple[bool, str]:
        if self.uses_local_whisper_transcriber():
            try:
                import faster_whisper  # noqa: F401
            except ImportError:
                return False, "faster-whisper is not installed"
            return True, f"faster-whisper model={self.config.whisper_model} will load on first voice"
        if self.uses_groq_transcriber():
            if not self.config.groq_api_key:
                return False, "GROQ_API_KEY is required for Groq voice transcription"
            return True, f"Groq audio model={self.config.groq_transcribe_model}"
        return False, f"Unsupported VOICE_TRANSCRIBER={self.config.voice_transcriber}"

    async def transcribe(self, path: Path) -> str:
        if self.uses_local_whisper_transcriber():
            async with self._voice_lock:
                return await self._transcribe_faster_whisper(path)
        if self.uses_groq_transcriber():
            if not self.config.groq_api_key:
                raise RuntimeError("GROQ_API_KEY is required for Groq voice transcription")
            return await self._transcribe_groq_audio(path)
        raise RuntimeError(f"Unsupported VOICE_TRANSCRIBER: {self.config.voice_transcriber}")

    async def _transcribe_groq_audio(self, path: Path) -> str:
        mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        form = aiohttp.FormData()
        form.add_field("model", self.config.groq_transcribe_model)
        if self.config.whisper_language:
            form.add_field("language", self.config.whisper_language)
        form.add_field("response_format", "json")
        form.add_field("file", path.read_bytes(), filename=path.name, content_type=mime_type)

        headers = {"Authorization": f"Bearer {self.config.groq_api_key}"}
        url = f"{self.config.groq_base_url.rstrip('/')}/openai/v1/audio/transcriptions"
        timeout = aiohttp.ClientTimeout(total=self.config.voice_processing_timeout_seconds)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, headers=headers, data=form) as response:
                text = await response.text()
                if response.status >= 400:
                    raise RuntimeError(f"Groq audio transcription failed with HTTP {response.status}: {text[:500]}")
                try:
                    data = json.loads(text)
                except json.JSONDecodeError as exc:
                    raise RuntimeError("Groq audio transcription returned non-JSON response") from exc

        transcript = self.clean_transcript(str(data.get("text") or ""))
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
            "title": title,
            "summary": None,
            "tldr": None,
            "full_text": None,
            "key_points": [],
            "open_questions": [],
            "next_step": None,
            "side_thoughts": None,
            "category": category or "Без категории",
            "tags": [],
        }

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
            "You are an editor for a personal idea archive. "
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

    def _prompt(self, text: str, source_type: str, has_photo: bool, size: str) -> str:
        if size == "short":
            shape = """
For a short idea fill:
- title: one line, in Russian
- summary: 2-3 sentences, in Russian
- tldr: null
- full_text: null
- key_points: []
- open_questions: []
- next_step: action item in Russian, or null
- side_thoughts: side thoughts in Russian, or null
- category: short category name in Russian
- tags: 3-5 Russian tags without #
"""
        else:
            shape = """
For a long detailed idea fill:
- title: one line, in Russian
- summary: null
- tldr: 2-3 sentences in Russian
- full_text: structured full transcript in Russian with headings and lists, do not drop important details
- key_points: key points as a list in Russian
- open_questions: doubts, uncertainties, "need to think", questions, in Russian
- next_step: next step/action item in Russian, or null
- side_thoughts: separate side-thoughts block in Russian if present, otherwise null
- category: short category name in Russian
- tags: 3-5 Russian tags without #
"""
        return f"""
Source: {source_type}
Photo as context: {"yes" if has_photo else "no"}

Task:
1. Remove filler sounds, hesitation noises and filler words such as "эээ", "ммм", "эм", "ну", "типа", "как бы", "короче"; preserve vivid meaningful phrasing.
2. Do not invent facts.
3. If the user was unsure, put that into open_questions.
4. If there is an action item, put it into next_step.
5. Return exactly these JSON keys:
title, summary, tldr, full_text, key_points, open_questions, next_step, side_thoughts, category, tags.
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
        return {
            "title": title,
            "summary": text[:700],
            "tldr": None,
            "full_text": None,
            "key_points": [],
            "open_questions": [],
            "next_step": None,
            "side_thoughts": None,
            "category": "Без категории",
            "tags": ["идея", "входящее", "без-разбора"],
        }

    def _normalize(self, data: dict[str, Any], text: str) -> dict[str, Any]:
        fallback = self._fallback(text)
        for key, value in fallback.items():
            data.setdefault(key, value)
        for key in ("key_points", "open_questions", "tags"):
            value = data.get(key)
            if isinstance(value, str):
                data[key] = [value]
            elif not isinstance(value, list):
                data[key] = []
        data["tags"] = [str(tag).strip().lstrip("#")[:40] for tag in data["tags"] if str(tag).strip()][:5]
        if not data["tags"]:
            data["tags"] = fallback["tags"]
        return data
