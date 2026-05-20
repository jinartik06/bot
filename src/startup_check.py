from __future__ import annotations

import asyncio
import logging

from .ai import IdeaAI
from .config import load_config


logger = logging.getLogger("ideas_bot.startup_check")


async def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config = load_config()
    ai = IdeaAI(config)

    logger.info(
        "Startup check voice config: transcriber=%s groq_base_url=%s groq_audio_model=%s language=%s timeout=%s",
        config.voice_transcriber,
        config.groq_base_url,
        config.groq_transcribe_model,
        config.whisper_language or "auto",
        config.voice_processing_timeout_seconds,
    )
    voice_ok, voice_detail = await ai.check_voice_transcriber()
    if not voice_ok:
        raise RuntimeError(voice_detail)

    logger.info("Startup check voice transcriber OK: %s", voice_detail)
    logger.info("Startup check finished without Telegram polling")


if __name__ == "__main__":
    asyncio.run(run())
