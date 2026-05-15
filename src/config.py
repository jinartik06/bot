from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


def _ids(value: str | None) -> set[int]:
    result: set[int] = set()
    for part in (value or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            result.add(int(part))
        except ValueError:
            raise ValueError(f"Telegram ID must be numeric: {part}") from None
    return result


def _groq_base_url(value: str | None) -> str:
    base_url = (value or "https://api.groq.com").strip().rstrip("/")
    suffix = "/openai/v1"
    if base_url.endswith(suffix):
        return base_url[: -len(suffix)]
    return base_url


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Config:
    bot_token: str
    groq_api_key: str | None
    groq_base_url: str
    admin_ids: set[int]
    allowed_ids: set[int]
    allow_all_users: bool
    database_path: Path
    default_timezone: str
    default_digest_weekday: int
    default_digest_time: str
    groq_text_model: str
    groq_transcribe_model: str
    voice_transcriber: str
    short_input_char_limit: int
    whisper_model: str = "small"
    whisper_device: str = "cpu"
    whisper_compute_type: str = "int8"
    whisper_cpu_threads: int = 2
    whisper_num_workers: int = 1
    whisper_language: str = "ru"
    whisper_beam_size: int = 5
    whisper_vad_filter: bool = False
    whisper_model_cache_dir: Path = Path("models")
    voice_lock_wait_seconds: int = 180
    voice_lock_ttl_seconds: int = 600
    ffmpeg_binary: str = "ffmpeg"
    media_command_timeout_seconds: int = 180
    voice_processing_timeout_seconds: int = 300


def load_config() -> Config:
    env_path = Path(__file__).resolve().parents[1] / ".env"
    load_dotenv(dotenv_path=env_path, override=True)

    bot_token = os.getenv("BOT_TOKEN", "").strip()
    if not bot_token:
        raise RuntimeError("BOT_TOKEN is required")

    admin_ids = _ids(os.getenv("ADMIN_TELEGRAM_IDS"))
    allowed_ids = _ids(os.getenv("ALLOWED_TELEGRAM_IDS")) | admin_ids
    allow_all_users = os.getenv("ALLOW_ALL_USERS", "false").strip().lower() in {"1", "true", "yes", "on"}

    return Config(
        bot_token=bot_token,
        groq_api_key=(os.getenv("GROQ_API_KEY") or "").strip() or None,
        groq_base_url=_groq_base_url(os.getenv("GROQ_BASE_URL")),
        admin_ids=admin_ids,
        allowed_ids=allowed_ids,
        allow_all_users=allow_all_users,
        database_path=Path(os.getenv("DATABASE_PATH", "data/ideas.db")),
        default_timezone=os.getenv("DEFAULT_TIMEZONE", "Europe/Moscow"),
        default_digest_weekday=int(os.getenv("DEFAULT_DIGEST_WEEKDAY", "6")),
        default_digest_time=os.getenv("DEFAULT_DIGEST_TIME", "19:00"),
        groq_text_model=os.getenv("GROQ_TEXT_MODEL", "qwen/qwen3-32b").strip(),
        groq_transcribe_model=os.getenv("GROQ_TRANSCRIBE_MODEL", "whisper-large-v3-turbo").strip(),
        voice_transcriber=os.getenv("VOICE_TRANSCRIBER", "faster_whisper").strip().lower(),
        short_input_char_limit=int(os.getenv("SHORT_INPUT_CHAR_LIMIT", "900")),
        whisper_model=os.getenv("WHISPER_MODEL", "small").strip(),
        whisper_device=os.getenv("WHISPER_DEVICE", "cpu").strip(),
        whisper_compute_type=os.getenv("WHISPER_COMPUTE_TYPE", "int8").strip(),
        whisper_cpu_threads=int(os.getenv("WHISPER_CPU_THREADS", "2")),
        whisper_num_workers=int(os.getenv("WHISPER_NUM_WORKERS", "1")),
        whisper_language=os.getenv("WHISPER_LANGUAGE", "ru").strip(),
        whisper_beam_size=int(os.getenv("WHISPER_BEAM_SIZE", "5")),
        whisper_vad_filter=_bool(os.getenv("WHISPER_VAD_FILTER"), False),
        whisper_model_cache_dir=Path(os.getenv("WHISPER_MODEL_CACHE_DIR", "models")),
        voice_lock_wait_seconds=int(os.getenv("VOICE_LOCK_WAIT_SECONDS", "180")),
        voice_lock_ttl_seconds=int(os.getenv("VOICE_LOCK_TTL_SECONDS", "600")),
        ffmpeg_binary=(os.getenv("FFMPEG_BINARY", "ffmpeg") or "ffmpeg").strip(),
        media_command_timeout_seconds=int(os.getenv("MEDIA_COMMAND_TIMEOUT_SECONDS", "180")),
        voice_processing_timeout_seconds=int(os.getenv("VOICE_PROCESSING_TIMEOUT_SECONDS", "300")),
    )
