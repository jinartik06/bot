from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse, urlunparse

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


def _running_in_docker() -> bool:
    return Path("/.dockerenv").exists() or os.getenv("RUNNING_IN_DOCKER", "").strip().lower() in {"1", "true", "yes", "on"}


def _t_one_ws_url(value: str | None) -> str:
    url = (value or "ws://127.0.0.1:8080/api/ws").strip()
    if not url:
        return ""
    parsed = urlparse(url)
    if _running_in_docker() and parsed.hostname in {"127.0.0.1", "localhost"} and (parsed.port in {8080, None}):
        return urlunparse(parsed._replace(netloc="t-one:8080"))
    return url


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
    voice_transcriber: str
    t_one_ws_url: str
    short_input_char_limit: int
    t_one_connect_timeout_seconds: int = 20
    t_one_receive_timeout_seconds: int = 30
    t_one_final_timeout_seconds: int = 90
    t_one_total_timeout_seconds: int = 240
    t_one_max_attempts: int = 2
    t_one_retry_delay_seconds: float = 2.0
    t_one_send_chunk_seconds: float = 5.0
    t_one_audio_filter: str | None = None
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
        voice_transcriber=os.getenv("VOICE_TRANSCRIBER", "t_one").strip().lower(),
        t_one_ws_url=_t_one_ws_url(os.getenv("T_ONE_WS_URL")),
        short_input_char_limit=int(os.getenv("SHORT_INPUT_CHAR_LIMIT", "900")),
        t_one_connect_timeout_seconds=int(os.getenv("T_ONE_CONNECT_TIMEOUT_SECONDS", "20")),
        t_one_receive_timeout_seconds=int(os.getenv("T_ONE_RECEIVE_TIMEOUT_SECONDS", "30")),
        t_one_final_timeout_seconds=int(os.getenv("T_ONE_FINAL_TIMEOUT_SECONDS", "90")),
        t_one_total_timeout_seconds=int(os.getenv("T_ONE_TOTAL_TIMEOUT_SECONDS", "240")),
        t_one_max_attempts=int(os.getenv("T_ONE_MAX_ATTEMPTS", "2")),
        t_one_retry_delay_seconds=float(os.getenv("T_ONE_RETRY_DELAY_SECONDS", "2")),
        t_one_send_chunk_seconds=float(os.getenv("T_ONE_SEND_CHUNK_SECONDS", "5")),
        t_one_audio_filter=(os.getenv("T_ONE_AUDIO_FILTER", "") or "").strip() or None,
        ffmpeg_binary=(os.getenv("FFMPEG_BINARY", "ffmpeg") or "ffmpeg").strip(),
        media_command_timeout_seconds=int(os.getenv("MEDIA_COMMAND_TIMEOUT_SECONDS", "180")),
        voice_processing_timeout_seconds=int(os.getenv("VOICE_PROCESSING_TIMEOUT_SECONDS", "300")),
    )
