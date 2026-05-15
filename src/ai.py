from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import aiohttp

from .config import Config


logger = logging.getLogger("ideas_bot.ai")


class IdeaAI:
    T_ONE_SAMPLE_RATE = 8000
    T_ONE_BYTES_PER_SAMPLE = 2
    T_ONE_PROTOCOL_CHUNK_BYTES = 2400 * T_ONE_BYTES_PER_SAMPLE
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
        self._t_one_lock = asyncio.Lock()

    def has_api(self) -> bool:
        return bool(self.config.groq_api_key)

    def uses_t_one_transcriber(self) -> bool:
        return self.config.voice_transcriber in {"t_one", "t-one", "tone"}

    def can_transcribe(self) -> bool:
        return self.uses_t_one_transcriber() and bool(self.config.t_one_ws_url)

    def t_one_is_busy(self) -> bool:
        return self._t_one_lock.locked()

    async def transcribe(self, path: Path) -> str:
        if not self.uses_t_one_transcriber():
            raise RuntimeError("Voice transcription is configured for T-one only")
        if not self.config.t_one_ws_url:
            raise RuntimeError("T_ONE_WS_URL is required for T-one voice transcription")
        return await self._transcribe_t_one(path)

    async def _transcribe_t_one(self, path: Path) -> str:
        pcm = await self._convert_to_t_one_pcm(path)
        if not pcm:
            raise RuntimeError("Audio conversion produced an empty PCM stream")

        last_error: Exception | None = None
        max_attempts = max(1, self.config.t_one_max_attempts)
        for attempt in range(1, max_attempts + 1):
            try:
                async with self._t_one_lock:
                    return await self._transcribe_t_one_pcm(pcm)
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "T-one transcription attempt %s/%s failed: %s",
                    attempt,
                    max_attempts,
                    exc,
                )
                if attempt < max_attempts:
                    await asyncio.sleep(max(0.0, self.config.t_one_retry_delay_seconds))

        raise RuntimeError("T-one transcription failed after retries") from last_error

    async def _transcribe_t_one_pcm(self, pcm: bytes) -> str:
        phrases: list[str] = []
        timeout = aiohttp.ClientTimeout(
            total=self.config.t_one_total_timeout_seconds,
            connect=self.config.t_one_connect_timeout_seconds,
            sock_connect=self.config.t_one_connect_timeout_seconds,
        )
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.ws_connect(self.config.t_one_ws_url, heartbeat=20) as ws:
                offset = 0
                sent_final = False
                chunks = self._t_one_send_chunks(pcm)
                deadline = asyncio.get_running_loop().time() + self.config.t_one_total_timeout_seconds
                logger.info(
                    "T-one stream started: pcm_bytes=%s send_chunks=%s send_chunk_seconds=%s",
                    len(pcm),
                    len(chunks),
                    self.config.t_one_send_chunk_seconds,
                )
                while True:
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        raise TimeoutError("T-one transcription timed out")
                    timeout_limit = (
                        self.config.t_one_final_timeout_seconds
                        if sent_final
                        else self.config.t_one_receive_timeout_seconds
                    )
                    wait_timeout = min(timeout_limit, remaining)
                    try:
                        message = await ws.receive(timeout=wait_timeout)
                    except (asyncio.TimeoutError, TimeoutError) as exc:
                        if sent_final:
                            break
                        if offset >= len(chunks):
                            sent_final = True
                            await ws.send_bytes(b"")
                            continue
                        if phrases:
                            logger.warning("T-one stopped sending ready events after producing transcript")
                            break
                        raise TimeoutError("T-one did not send a ready/transcript event in time") from exc
                    if message.type == aiohttp.WSMsgType.TEXT:
                        try:
                            data = json.loads(message.data)
                        except json.JSONDecodeError:
                            logger.warning("T-one returned non-JSON websocket message")
                            continue
                        event = data.get("event")
                        if event == "ready":
                            if offset < len(chunks):
                                await ws.send_bytes(chunks[offset])
                                logger.debug(
                                    "T-one audio chunk sent: chunk=%s/%s bytes=%s",
                                    offset + 1,
                                    len(chunks),
                                    len(chunks[offset]),
                                )
                                offset += 1
                            elif not sent_final:
                                sent_final = True
                                await ws.send_bytes(b"")
                        elif event == "transcript":
                            text = self.clean_transcript((data.get("phrase") or {}).get("text") or "")
                            if text:
                                phrases.append(text)
                        elif event == "error":
                            raise RuntimeError(f"T-one returned error event: {data}")
                    elif message.type in {aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR}:
                        if not sent_final and not phrases:
                            raise RuntimeError("T-one websocket closed before all audio was sent")
                        if ws.exception():
                            raise RuntimeError("T-one websocket error") from ws.exception()
                        break

        transcript = self.clean_transcript(" ".join(phrases))
        if not transcript:
            raise RuntimeError("T-one returned an empty transcript")
        return transcript

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

    def _t_one_send_chunks(self, pcm: bytes) -> list[bytes]:
        if len(pcm) % self.T_ONE_BYTES_PER_SAMPLE:
            pcm += b"\x00"
        chunk_bytes = self._t_one_send_chunk_bytes()
        chunks: list[bytes] = []
        for offset in range(0, len(pcm), chunk_bytes):
            chunks.append(pcm[offset : offset + chunk_bytes])
        return chunks

    def _t_one_send_chunk_bytes(self) -> int:
        seconds = max(0.3, float(self.config.t_one_send_chunk_seconds))
        raw_bytes = int(self.T_ONE_SAMPLE_RATE * self.T_ONE_BYTES_PER_SAMPLE * seconds)
        raw_bytes -= raw_bytes % self.T_ONE_BYTES_PER_SAMPLE
        return max(self.T_ONE_PROTOCOL_CHUNK_BYTES, raw_bytes)

    async def _convert_to_t_one_pcm(self, path: Path) -> bytes:
        with tempfile.TemporaryDirectory() as tmpdir:
            pcm_path = Path(tmpdir) / "t_one_input.s16le"
            stderr = await self._run_t_one_ffmpeg(path, pcm_path, self.config.t_one_audio_filter)
            if not pcm_path.exists():
                raise RuntimeError("ffmpeg did not create PCM output for T-one")
            pcm = pcm_path.read_bytes()
            duration = len(pcm) / (self.T_ONE_SAMPLE_RATE * self.T_ONE_BYTES_PER_SAMPLE)
            logger.info(
                "Audio prepared for T-one: source=%s pcm_bytes=%s duration_seconds=%.2f ffmpeg_stderr_bytes=%s",
                path.suffix or "unknown",
                len(pcm),
                duration,
                len(stderr),
            )
            return pcm

    async def _run_t_one_ffmpeg(self, source: Path, pcm_path: Path, audio_filter: str | None) -> bytes:
        try:
            return await self._run_t_one_ffmpeg_once(source, pcm_path, audio_filter)
        except RuntimeError as exc:
            if not audio_filter:
                raise
            logger.warning("ffmpeg with T-one audio filter failed, retrying without filter: %s", exc)
            return await self._run_t_one_ffmpeg_once(source, pcm_path, None)

    async def _run_t_one_ffmpeg_once(self, source: Path, pcm_path: Path, audio_filter: str | None) -> bytes:
        executable = self.config.ffmpeg_binary or "ffmpeg"
        try:
            return await self._run_t_one_ffmpeg_command(
                self._t_one_ffmpeg_args(executable, source, pcm_path, audio_filter)
            )
        except FileNotFoundError as exc:
            fallback = self._bundled_ffmpeg_binary()
            if not fallback or fallback == executable:
                raise RuntimeError(
                    "ffmpeg is not installed or not available in PATH. "
                    "Install ffmpeg or reinstall Python dependencies so imageio-ffmpeg is available."
                ) from exc
            logger.warning("ffmpeg binary %r was not found, retrying with bundled imageio-ffmpeg binary", executable)
            try:
                return await self._run_t_one_ffmpeg_command(
                    self._t_one_ffmpeg_args(fallback, source, pcm_path, audio_filter)
                )
            except FileNotFoundError as fallback_exc:
                raise RuntimeError("ffmpeg is not installed or not available in PATH") from fallback_exc

    def _t_one_ffmpeg_args(
        self,
        executable: str,
        source: Path,
        pcm_path: Path,
        audio_filter: str | None,
    ) -> list[str]:
        args = [
            executable,
            "-y",
            "-i",
            str(source),
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(self.T_ONE_SAMPLE_RATE),
        ]
        if audio_filter:
            args.extend(["-af", audio_filter])
        args.extend([
            "-f",
            "s16le",
            "-acodec",
            "pcm_s16le",
            str(pcm_path),
        ])
        return args

    async def _run_t_one_ffmpeg_command(self, args: list[str]) -> bytes:
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
            raise RuntimeError("ffmpeg timed out while preparing audio for T-one") from exc
        if process.returncode != 0:
            detail = stderr.decode("utf-8", errors="ignore")[-500:]
            raise RuntimeError(f"ffmpeg failed to prepare audio for T-one: {detail}")
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
