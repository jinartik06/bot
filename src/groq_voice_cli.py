from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

from .ai import IdeaAI
from .config import load_config
from .groq_voice import GroqVoiceTranscriber


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Transcribe one audio file through Groq audio/transcriptions.")
    parser.add_argument("audio_file", type=Path, nargs="?", help="Path to .ogg, .mp3, .wav, .m4a or another audio file")
    parser.add_argument("--check-only", action="store_true", help="Only check Groq startup connectivity and settings")
    parser.add_argument("--skip-check", action="store_true", help="Skip Groq startup /models check")
    return parser


async def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = build_parser().parse_args()
    config = load_config()
    transcriber = GroqVoiceTranscriber(config)

    if not args.skip_check:
        ok, detail = await transcriber.check_startup()
        if not ok:
            raise RuntimeError(detail)
        logging.getLogger("ideas_bot.groq_voice_cli").info(detail)

    if args.check_only:
        return
    if not args.audio_file:
        raise RuntimeError("audio_file is required unless --check-only is used")

    text = await transcriber.transcribe(args.audio_file)
    print(IdeaAI(config).clean_transcript(text))


if __name__ == "__main__":
    asyncio.run(run())
