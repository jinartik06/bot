from __future__ import annotations

import asyncio
import html
import logging
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiohttp

if __package__ in {None, ""}:
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = "src"

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BotCommand, BotCommandScopeChat, CallbackQuery, Message, TelegramObject
from aiogram.utils.markdown import hbold, hcode
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from .ai import IdeaAI
from .config import Config, load_config
from .db import Database
from .keyboards import (
    album_photo_actions,
    admin_menu,
    archived_idea_actions,
    categories_menu,
    idea_actions,
    main_menu,
    next_step_actions,
    periods_menu,
    settings_menu,
    start_menu,
)
from .render import album_caption, album_list_text, compact_list, digest_text, idea_details_text, idea_text, next_step_item_text, next_steps_text, period_since, start_text, usage_help


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("ideas_bot")
router = Router()
VOICE_RUNTIME_LOCK = "voice_transcription"


class Form(StatesGroup):
    waiting_name = State()
    search_query = State()
    continue_idea = State()
    add_allowed = State()
    remove_allowed = State()
    block_user = State()
    unblock_user = State()
    rename_title = State()
    set_category = State()
    settings_time = State()
    settings_timezone = State()
    add_category = State()


class AccessMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        message = event if isinstance(event, Message) else None
        callback = event if isinstance(event, CallbackQuery) else None
        user = data.get("event_from_user")
        if not user:
            return await handler(event, data)

        db: Database = data["db"]
        config: Config = data["config"]
        await db.upsert_user(
            telegram_id=user.id,
            username=user.username,
            display_name=user.full_name,
            default_timezone=config.default_timezone,
            default_weekday=config.default_digest_weekday,
            default_time=config.default_digest_time,
        )

        is_admin = await db.is_admin(user.id)
        if await db.is_blocked(user.id) and not is_admin:
            text = "Доступ закрыт администратором."
            if callback:
                await callback.answer("Доступ закрыт", show_alert=True)
                if callback.message:
                    await callback.message.answer(text)
            elif message:
                await message.answer(text)
            return None

        if config.allow_all_users or is_admin or await db.is_allowed(user.id):
            return await handler(event, data)

        text = (
            "Доступ закрыт. Отправьте администратору ваш Telegram ID:\n"
            f"{hcode(str(user.id))}"
        )
        if callback:
            await callback.answer("Нет доступа", show_alert=True)
            if callback.message:
                await callback.message.answer(text)
        elif message:
            await message.answer(text)
        return None


async def send_chunks(target: Message, text: str, **kwargs: Any) -> None:
    reply_markup = kwargs.pop("reply_markup", None)
    if len(text) <= 3900:
        await target.answer(text, reply_markup=reply_markup, **kwargs)
        return

    plain_text = html.unescape(re.sub(r"</?(?:b|i|u|s|code|pre)>", "", text))
    chunks = []
    while plain_text:
        if len(plain_text) <= 3900:
            chunks.append(plain_text)
            break
        split_at = plain_text.rfind("\n", 0, 3900)
        if split_at < 1200:
            split_at = 3900
        chunks.append(plain_text[:split_at].strip())
        plain_text = plain_text[split_at:].strip()

    for index, chunk in enumerate(chunks):
        markup = reply_markup if index == len(chunks) - 1 else None
        await target.answer(chunk, reply_markup=markup, parse_mode=None, **kwargs)


CATEGORY_ONLY_RE = re.compile(
    r"^\s*(?:category|категория|в категорию|добавь(?: это)? в категорию|отнеси(?: это)? в категорию)\s*[:\-–—]?\s*(?P<name>[^#\n\r]{2,80})\s*$",
    re.IGNORECASE,
)
CATEGORY_LINE_RE = re.compile(
    r"^\s*(?:category|категория)\s*[:\-–—]?\s*(?P<name>[^#\n\r]{2,80})\s*$",
    re.IGNORECASE,
)
TRAILING_CATEGORY_RE = re.compile(
    r"(?is)^(?P<body>.+?)\s+(?:category|категория|в категорию|добавь(?: это)? в категорию|отнеси(?: это)? в категорию)\s*[:\-–—]?\s*(?P<name>[^#\n\r]{2,80})\s*$",
)
URL_RE = re.compile(r"https?://[^\s<>()]+", re.IGNORECASE)
TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
PHOTO_WITHOUT_TEXT = "Фото без подписи, AI-описания и распознанного текста."


def clean_category_name(value: str | None) -> str | None:
    if not value:
        return None
    clean = " ".join(value.strip().strip(" .,!?:;\"'«»()[]").lstrip("#").split())
    return clean[:80] or None


def split_category_hint(text: str) -> tuple[str, str | None]:
    clean = text.strip()
    if not clean:
        return "", None

    lines = clean.splitlines()
    kept_lines: list[str] = []
    category: str | None = None
    for line in lines:
        match = CATEGORY_LINE_RE.match(line)
        if match:
            category = clean_category_name(match.group("name"))
            continue
        kept_lines.append(line)
    if category:
        return "\n".join(kept_lines).strip(), category

    match = TRAILING_CATEGORY_RE.match(clean)
    if match:
        return match.group("body").strip(), clean_category_name(match.group("name"))

    match = CATEGORY_ONLY_RE.match(clean)
    if match:
        return "", clean_category_name(match.group("name"))

    return clean, None


def category_name_from_chat_text(text: str | None) -> str | None:
    body, category = split_category_hint(text or "")
    if category and not body:
        return category
    return clean_category_name(text)


def first_url(text: str) -> str | None:
    match = URL_RE.search(text)
    return match.group(0).rstrip(".,);]") if match else None


async def fetch_link_title(url: str) -> str | None:
    timeout = aiohttp.ClientTimeout(total=8)
    headers = {"User-Agent": "ideas-thoughts-bot/1.0"}
    try:
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.get(url, allow_redirects=True) as response:
                if response.status >= 400:
                    return None
                content_type = response.headers.get("content-type", "")
                if "html" not in content_type.lower():
                    return None
                raw = await response.content.read(200_000)
                charset = response.charset or "utf-8"
    except Exception:
        logger.info("Could not fetch link title: %s", url, exc_info=True)
        return None

    page = raw.decode(charset, errors="ignore")
    match = TITLE_RE.search(page)
    if not match:
        return None
    title = html.unescape(re.sub(r"\s+", " ", match.group(1))).strip()
    return title[:180] or None


async def enrich_link_text(text: str) -> str:
    url = first_url(text)
    if not url:
        return text
    title = await fetch_link_title(url)
    if not title:
        return text
    return f"{text.strip()}\n\nЗаголовок ссылки: {title}"


def safe_photo_name(user_id: int, file_unique_id: str | None, suffix: str) -> Path:
    clean_id = re.sub(r"[^A-Za-z0-9_-]+", "_", file_unique_id or "photo").strip("_") or "photo"
    clean_suffix = suffix.lower() if suffix.lower() in PHOTO_EXTENSIONS else ".jpg"
    return Path(str(user_id)) / f"{clean_id}{clean_suffix}"


async def run_photo_ocr(path: Path, config: Config) -> str | None:
    if not config.photo_ocr_enabled:
        return None
    args = [
        config.tesseract_binary or "tesseract",
        str(path),
        "stdout",
        "-l",
        config.photo_ocr_language or "rus+eng",
    ]
    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=max(1, config.photo_ocr_timeout_seconds),
        )
    except FileNotFoundError:
        logger.info("Photo OCR skipped: tesseract binary was not found")
        return None
    except asyncio.TimeoutError:
        if "process" in locals() and process.returncode is None:
            process.kill()
            await process.wait()
        logger.warning("Photo OCR timed out: path=%s", path)
        return None

    if process.returncode != 0:
        detail = stderr.decode("utf-8", errors="ignore")[-300:]
        logger.info("Photo OCR skipped: tesseract failed with code=%s detail=%s", process.returncode, detail)
        return None
    text = " ".join(stdout.decode("utf-8", errors="ignore").split())
    return text[:2000] or None


async def save_photo_and_ocr(message: Message, bot: Bot, config: Config) -> tuple[str | None, str | None, str | None]:
    if not message.photo:
        return None, None, None

    photo = message.photo[-1]
    try:
        tg_file = await bot.get_file(photo.file_id)
        if not tg_file.file_path:
            raise RuntimeError("Telegram returned an empty photo file path")
        suffix = Path(tg_file.file_path).suffix or ".jpg"
        relative_path = safe_photo_name(message.from_user.id, getattr(photo, "file_unique_id", None), suffix)
        target_path = config.photo_storage_dir / relative_path
        target_path.parent.mkdir(parents=True, exist_ok=True)
        await bot.download_file(tg_file.file_path, destination=target_path)
    except Exception:
        logger.exception("Failed to save Telegram photo")
        return photo.file_id, None, None

    ocr_text = await run_photo_ocr(target_path, config)
    return photo.file_id, str(target_path), ocr_text


def merge_photo_text(caption: str, ocr_text: str | None, vision_text: str | None = None) -> str:
    caption = caption.strip()
    ocr_text = " ".join(str(ocr_text or "").split())
    vision_text = " ".join(str(vision_text or "").split())
    parts = []
    if caption:
        parts.append(f"Подпись к фото: {caption}")
    if vision_text:
        parts.append(f"AI-описание фото: {vision_text}")
    if ocr_text:
        parts.append(f"Текст, распознанный на фото: {ocr_text}")
    return "\n\n".join(parts) if parts else PHOTO_WITHOUT_TEXT


def photo_has_text_context(caption: str, ocr_text: str | None, vision_text: str | None = None) -> bool:
    return bool(caption.strip() or str(ocr_text or "").strip() or str(vision_text or "").strip())


def delete_local_photo(photo_path: str | None, config: Config) -> bool:
    if not photo_path:
        return False
    try:
        target = Path(photo_path).resolve()
        base = config.photo_storage_dir.resolve()
        target.relative_to(base)
    except (OSError, ValueError):
        logger.warning("Refusing to delete photo outside storage dir: %s", photo_path)
        return False

    try:
        if target.is_file():
            target.unlink()
            return True
    except OSError:
        logger.exception("Failed to delete local photo file: %s", target)
    return False


async def send_idea(message: Message, row) -> None:
    await send_chunks(message, idea_text(row), reply_markup=idea_actions(row["id"]))


async def send_archived_idea(message: Message, row) -> None:
    await send_chunks(message, idea_text(row), reply_markup=archived_idea_actions(row["id"]))


async def send_next_steps(message: Message, rows) -> None:
    if not rows:
        await message.answer(next_steps_text(rows))
        return
    await message.answer(next_steps_text(rows))
    for row in rows:
        await send_chunks(message, next_step_item_text(row), reply_markup=next_step_actions(row["id"]))


async def send_album_photo(message: Message, row) -> None:
    caption = album_caption(row)
    markup = album_photo_actions(row["id"])
    if row["photo_file_id"]:
        try:
            await message.answer_photo(row["photo_file_id"], caption=caption, reply_markup=markup)
            return
        except TelegramBadRequest as exc:
            logger.warning("Could not send album photo, falling back to text: %s", exc)
    await send_chunks(message, caption, reply_markup=markup)


def build_continuation_text(original_text: str, continuation: str) -> str:
    original = original_text.strip()
    addition = continuation.strip()
    if not original:
        return addition
    return f"Исходная мысль:\n{original}\n\nПродолжение пользователя:\n{addition}"


async def delete_status_message(message: Message | None) -> None:
    if not message:
        return
    try:
        await message.delete()
    except TelegramBadRequest as exc:
        logger.debug("Could not delete status message: %s", exc)


async def edit_status_message(message: Message | None, text: str, fallback: Message) -> None:
    if message:
        try:
            await message.edit_text(text)
            return
        except TelegramBadRequest as exc:
            logger.debug("Could not edit status message, sending fallback: %s", exc)
    await fallback.answer(text)


async def edit_or_send_idea(callback: CallbackQuery, row) -> None:
    if not callback.message:
        return
    text = idea_text(row)
    markup = idea_actions(row["id"])
    if len(text) <= 3900:
        try:
            await callback.message.edit_text(text, reply_markup=markup)
            return
        except TelegramBadRequest as exc:
            if "message is not modified" in str(exc).lower():
                return
            logger.warning("Could not edit idea message, sending a new one: %s", exc)
    await send_chunks(callback.message, text, reply_markup=markup)


async def set_latest_idea_category(message: Message, db: Database, category: str) -> bool:
    row = await db.latest_idea(message.from_user.id)
    if not row:
        return False
    await db.update_idea_category(message.from_user.id, row["id"], category)
    await message.answer(f"Категория для последней мысли: {html.escape(category)}.")
    return True


async def register_seen_user(message: Message, db: Database, config: Config) -> None:
    user = message.from_user
    if not user:
        return
    await db.upsert_user(
        telegram_id=user.id,
        username=user.username,
        display_name=user.full_name,
        default_timezone=config.default_timezone,
        default_weekday=config.default_digest_weekday,
        default_time=config.default_digest_time,
    )


async def transcribe_with_voice_runtime_lock(
    message: Message,
    db: Database,
    ai: IdeaAI,
    config: Config,
    path: Path,
) -> str:
    owner = f"{message.chat.id}:{message.message_id}:{message.from_user.id}"
    deadline = asyncio.get_running_loop().time() + max(1, config.voice_lock_wait_seconds)
    waiting_logged = False

    while True:
        if await db.try_acquire_runtime_lock(
            VOICE_RUNTIME_LOCK,
            owner,
            max(60, config.voice_lock_ttl_seconds),
        ):
            logger.info("Voice runtime lock acquired: owner=%s", owner)
            try:
                return await ai.transcribe(path)
            finally:
                released = await db.release_runtime_lock(VOICE_RUNTIME_LOCK, owner)
                logger.info("Voice runtime lock released: owner=%s released=%s", owner, released)

        if not waiting_logged:
            waiting_logged = True
            logger.info("Voice runtime lock is busy, waiting: owner=%s", owner)

        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError("Voice transcriber stayed busy for too long")

        await asyncio.sleep(2)


def parse_admin_user_input(text: str | None) -> tuple[int, str | None]:
    parts = (text or "").strip().split(maxsplit=1)
    if not parts:
        raise ValueError("empty")
    telegram_id = int(parts[0])
    reason = parts[1].strip() if len(parts) > 1 else None
    return telegram_id, reason or None


def format_admin_users(rows) -> str:
    lines = [hbold("Пользователи")]
    if not rows:
        lines.append("Пока никто не писал боту.")
        return "\n".join(lines)
    for row in rows:
        statuses = []
        if row["is_admin"]:
            statuses.append("admin")
        if row["is_allowed"]:
            statuses.append("allowed")
        if row["is_blocked"]:
            statuses.append("blocked")
        status = ", ".join(statuses) if statuses else "seen"
        username = f" @{html.escape(row['username'])}" if row["username"] else ""
        name = f" - {html.escape(row['display_name'])}" if row["display_name"] else ""
        reason = f"; reason: {html.escape(row['block_reason'])}" if row["block_reason"] else ""
        seen = f"; seen: {html.escape(str(row['updated_at'])[:19])}" if row["updated_at"] else ""
        lines.append(
            f"- {hcode(str(row['telegram_id']))}{username}{name} "
            f"[{status}; ideas: {row['ideas_count']}{seen}{reason}]"
        )
    return "\n".join(lines)


def format_blocked_users(rows) -> str:
    lines = [hbold("Blacklist")]
    if not rows:
        lines.append("Черный список пуст.")
        return "\n".join(lines)
    for row in rows:
        username = f" @{html.escape(row['username'])}" if row["username"] else ""
        name = f" - {html.escape(row['display_name'])}" if row["display_name"] else ""
        reason = f" Причина: {html.escape(row['reason'])}." if row["reason"] else ""
        lines.append(f"- {hcode(str(row['telegram_id']))}{username}{name}.{reason}")
    return "\n".join(lines)


@router.message(Command("start"))
async def start(message: Message, state: FSMContext, db: Database, config: Config) -> None:
    await register_seen_user(message, db, config)
    await state.clear()
    user = await db.get_user(message.from_user.id)
    await message.answer(start_text(), reply_markup=start_menu(user.is_admin if user else False))


@router.message(Form.waiting_name, F.text)
async def set_name(message: Message, state: FSMContext, db: Database) -> None:
    name = (message.text or "").strip()
    if not name:
        await message.answer("Напиши имя текстом.")
        return
    await db.update_display_name(message.from_user.id, name[:80])
    await state.clear()
    user = await db.get_user(message.from_user.id)
    await message.answer(f"Готово, {html.escape(name[:80])}. Теперь можно отправлять идеи.", reply_markup=main_menu(user.is_admin if user else False))


@router.message(Command("help"))
async def help_cmd(message: Message) -> None:
    await message.answer(usage_help())


@router.message(Command("list", "thoughts"))
async def list_cmd(message: Message, db: Database) -> None:
    rows = await db.list_ideas(message.from_user.id, 20)
    if not rows:
        await message.answer("Пока мыслей нет.")
        return
    for row in rows:
        await send_idea(message, row)


@router.message(Command("search"))
async def search_cmd(message: Message, command: CommandObject, db: Database) -> None:
    query = (command.args or "").strip()
    if not query:
        await message.answer(f"Напиши запрос: {hcode('/search упаковка')}")
        return
    rows = await db.search_ideas(message.from_user.id, query)
    if not rows:
        await message.answer("Ничего не нашёл.")
        return
    for row in rows:
        await send_idea(message, row)


@router.message(Command("next", "steps"))
async def next_cmd(message: Message, db: Database) -> None:
    rows = await db.next_step_ideas(message.from_user.id)
    await send_next_steps(message, rows)


@router.message(Command("archive"))
async def archive_cmd(message: Message, db: Database) -> None:
    rows = await db.archived_ideas(message.from_user.id)
    if not rows:
        await message.answer("Архив пуст.")
        return
    for row in rows:
        await send_archived_idea(message, row)


@router.message(Command("album"))
async def album_cmd(message: Message, db: Database) -> None:
    rows = await db.album_photos(message.from_user.id)
    if not rows:
        await message.answer("Альбом пока пуст.")
        return
    await message.answer(album_list_text(rows))
    for row in rows:
        await send_album_photo(message, row)


@router.message(Command("today", "week", "month"))
async def period_cmd(message: Message, db: Database) -> None:
    period = message.text.split()[0].lstrip("/")
    user = await db.get_user(message.from_user.id)
    rows = await db.ideas_since(message.from_user.id, period_since(period, user.timezone if user else "Europe/Moscow"))
    await message.answer(compact_list(rows))


@router.message(Command("category", "categories"))
async def categories_cmd(message: Message, db: Database) -> None:
    categories = await db.list_categories(message.from_user.id)
    await message.answer("Категории:", reply_markup=categories_menu(categories))


@router.message(Command("settings"))
async def settings_cmd(message: Message, db: Database) -> None:
    user = await db.get_user(message.from_user.id)
    if not user:
        return
    await message.answer(
        f"Дайджест: {'включён' if user.digest_enabled else 'выключен'}\n"
        f"Время: {hcode(user.digest_time)}, день недели: воскресенье\n"
        f"Часовой пояс: {hcode(user.timezone)}",
        reply_markup=settings_menu(user.digest_enabled),
    )


@router.message(Command("admin"))
async def admin_cmd(message: Message, db: Database) -> None:
    if not await db.is_admin(message.from_user.id):
        await message.answer("Эта команда только для администратора.")
        return
    await message.answer("Админка пользователей:", reply_markup=admin_menu())


@router.callback_query(F.data == "nav:menu")
async def nav_menu(callback: CallbackQuery, db: Database) -> None:
    user = await db.get_user(callback.from_user.id)
    await callback.message.edit_text("Главное меню", reply_markup=main_menu(user.is_admin if user else False))
    await callback.answer()


@router.callback_query(F.data == "nav:add")
async def nav_add(callback: CallbackQuery) -> None:
    await callback.message.answer("Просто отправь сюда текст, голосовое, фото, ссылку или пересланное сообщение. Я сам разберу это на карточки.")
    await callback.answer()


@router.callback_query(F.data == "nav:how")
async def nav_how(callback: CallbackQuery) -> None:
    await callback.message.answer(usage_help())
    await callback.answer()


@router.callback_query(F.data == "nav:list")
async def nav_list(callback: CallbackQuery, db: Database) -> None:
    rows = await db.list_ideas(callback.from_user.id, 20)
    if not rows:
        await callback.message.answer("Пока мыслей нет.")
    for row in rows:
        await send_idea(callback.message, row)
    await callback.answer()


@router.callback_query(F.data == "nav:steps")
async def nav_steps(callback: CallbackQuery, db: Database) -> None:
    rows = await db.next_step_ideas(callback.from_user.id)
    await send_next_steps(callback.message, rows)
    await callback.answer()


@router.callback_query(F.data == "nav:archive")
async def nav_archive(callback: CallbackQuery, db: Database) -> None:
    rows = await db.archived_ideas(callback.from_user.id)
    if not rows:
        await callback.message.answer("Архив пуст.")
    for row in rows:
        await send_archived_idea(callback.message, row)
    await callback.answer()


@router.callback_query(F.data == "nav:album")
async def nav_album(callback: CallbackQuery, db: Database) -> None:
    rows = await db.album_photos(callback.from_user.id)
    if not rows:
        await callback.message.answer("Альбом пока пуст.")
        await callback.answer()
        return
    await callback.message.answer(album_list_text(rows))
    for row in rows:
        await send_album_photo(callback.message, row)
    await callback.answer()


@router.callback_query(F.data == "nav:search")
async def nav_search(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Form.search_query)
    await callback.message.answer("Напиши поисковый запрос.")
    await callback.answer()


@router.message(Form.search_query, F.text)
async def nav_search_text(message: Message, state: FSMContext, db: Database) -> None:
    query = (message.text or "").strip()
    if not query:
        await message.answer("Нужен текстовый запрос.")
        return
    rows = await db.search_ideas(message.from_user.id, query)
    await state.clear()
    if not rows:
        await message.answer("Ничего не нашёл.")
        return
    for row in rows:
        await send_idea(message, row)


@router.callback_query(F.data == "nav:periods")
async def nav_periods(callback: CallbackQuery) -> None:
    await callback.message.edit_text("Выбери период:", reply_markup=periods_menu())
    await callback.answer()


@router.callback_query(F.data.startswith("period:"))
async def nav_period(callback: CallbackQuery, db: Database) -> None:
    period = callback.data.split(":")[1]
    user = await db.get_user(callback.from_user.id)
    rows = await db.ideas_since(callback.from_user.id, period_since(period, user.timezone if user else "Europe/Moscow"))
    await callback.message.answer(compact_list(rows))
    await callback.answer()


@router.callback_query(F.data == "nav:categories")
async def nav_categories(callback: CallbackQuery, db: Database) -> None:
    categories = await db.list_categories(callback.from_user.id)
    await callback.message.edit_text("Категории:", reply_markup=categories_menu(categories))
    await callback.answer()


@router.callback_query(F.data.startswith("cat:view:"))
async def cat_view(callback: CallbackQuery, db: Database) -> None:
    category_id = int(callback.data.split(":")[2])
    rows = await db.ideas_by_category(callback.from_user.id, category_id)
    await callback.message.answer(compact_list(rows))
    await callback.answer()


@router.callback_query(F.data == "cat:add")
async def cat_add(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Form.add_category)
    await callback.message.answer("Напиши название новой категории.")
    await callback.answer()


@router.message(Form.add_category, F.text)
async def cat_add_text(message: Message, state: FSMContext, db: Database) -> None:
    name = category_name_from_chat_text(message.text)
    if not name:
        await message.answer("Нужно название текстом.")
        return
    await db.ensure_category(message.from_user.id, name)
    await state.clear()
    await message.answer("Категория добавлена.")


@router.callback_query(F.data == "nav:settings")
async def nav_settings(callback: CallbackQuery, db: Database) -> None:
    user = await db.get_user(callback.from_user.id)
    await callback.message.edit_text(
        f"Дайджест: {'включён' if user.digest_enabled else 'выключен'}\n"
        f"Время: {hcode(user.digest_time)}\n"
        f"Часовой пояс: {hcode(user.timezone)}",
        reply_markup=settings_menu(user.digest_enabled),
    )
    await callback.answer()


@router.callback_query(F.data == "settings:toggle_digest")
async def toggle_digest(callback: CallbackQuery, db: Database) -> None:
    user = await db.get_user(callback.from_user.id)
    await db.update_settings(callback.from_user.id, digest_enabled=0 if user.digest_enabled else 1)
    updated = await db.get_user(callback.from_user.id)
    await callback.message.edit_text(
        f"Дайджест: {'включён' if updated.digest_enabled else 'выключен'}\n"
        f"Время: {hcode(updated.digest_time)}\n"
        f"Часовой пояс: {hcode(updated.timezone)}",
        reply_markup=settings_menu(updated.digest_enabled),
    )
    await callback.answer("Сохранено")


@router.callback_query(F.data == "settings:time")
async def settings_time(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Form.settings_time)
    await callback.message.answer("Напиши время дайджеста в формате HH:MM, например 19:00.")
    await callback.answer()


@router.message(Form.settings_time, F.text)
async def settings_time_text(message: Message, state: FSMContext, db: Database) -> None:
    value = (message.text or "").strip()
    try:
        datetime.strptime(value, "%H:%M")
    except ValueError:
        await message.answer("Формат должен быть HH:MM, например 19:00.")
        return
    await db.update_settings(message.from_user.id, digest_time=value)
    await state.clear()
    await message.answer("Время дайджеста сохранено.")


@router.callback_query(F.data == "settings:timezone")
async def settings_timezone(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Form.settings_timezone)
    await callback.message.answer("Напиши часовой пояс IANA, например Europe/Moscow или Asia/Yerevan.")
    await callback.answer()


@router.message(Form.settings_timezone, F.text)
async def settings_timezone_text(message: Message, state: FSMContext, db: Database) -> None:
    value = (message.text or "").strip()
    try:
        ZoneInfo(value)
    except ZoneInfoNotFoundError:
        await message.answer("Не нашёл такой часовой пояс. Пример: Europe/Moscow.")
        return
    await db.update_settings(message.from_user.id, timezone=value)
    await state.clear()
    await message.answer("Часовой пояс сохранён.")


@router.callback_query(F.data.startswith("idea:details:"))
async def show_details(callback: CallbackQuery, db: Database) -> None:
    idea_id = int(callback.data.split(":")[2])
    row = await db.get_idea(callback.from_user.id, idea_id)
    if row:
        await send_chunks(callback.message, idea_details_text(row))
    await callback.answer()


@router.callback_query(F.data.startswith("idea:archive:"))
async def archive_idea(callback: CallbackQuery, db: Database) -> None:
    idea_id = int(callback.data.split(":")[2])
    await db.archive_idea(callback.from_user.id, idea_id)
    await callback.message.edit_text("Запись убрана в архив.")
    await callback.answer()


@router.callback_query(F.data.startswith("idea:restore:"))
async def restore_idea(callback: CallbackQuery, db: Database) -> None:
    idea_id = int(callback.data.split(":")[2])
    await db.restore_idea(callback.from_user.id, idea_id)
    row = await db.get_idea(callback.from_user.id, idea_id)
    if row:
        await callback.message.edit_text(idea_text(row), reply_markup=idea_actions(row["id"]))
    await callback.answer("Вернул в мысли")


@router.callback_query(F.data.startswith("photo:delete:"))
async def delete_photo(callback: CallbackQuery, db: Database, config: Config) -> None:
    idea_id = int(callback.data.split(":")[2])
    row = await db.remove_idea_photo(callback.from_user.id, idea_id)
    if not row:
        await callback.answer("Фото уже удалено", show_alert=True)
        return
    deleted_file = delete_local_photo(row["photo_path"], config)
    text = "Фото удалено из альбома."
    if deleted_file:
        text += " Локальный файл тоже удалён."
    if callback.message:
        try:
            if callback.message.photo:
                await callback.message.edit_caption(text)
            else:
                await callback.message.edit_text(text)
        except TelegramBadRequest:
            await callback.message.answer(text)
    await callback.answer("Удалено")


@router.callback_query(F.data.startswith("idea:original:"))
async def show_original(callback: CallbackQuery, db: Database) -> None:
    idea_id = int(callback.data.split(":")[2])
    row = await db.get_idea(callback.from_user.id, idea_id)
    if row:
        await callback.message.answer(f"{hbold('Оригинал')}\n{html.escape(row['original_text'])}")
    await callback.answer()


@router.callback_query(F.data.startswith("idea:analyze:"))
async def analyze_idea(callback: CallbackQuery, db: Database, ai: IdeaAI) -> None:
    idea_id = int(callback.data.split(":")[2])
    row = await db.get_idea(callback.from_user.id, idea_id)
    if not row:
        await callback.answer("Мысль не найдена", show_alert=True)
        return
    await callback.answer("Готовлю анализ...")
    try:
        payload = await ai.structure_idea(
            row["original_text"],
            row["source_type"],
            bool(row["photo_file_id"]),
            allow_fallback=False,
        )
    except Exception:
        logger.exception("Idea analysis failed")
        await callback.message.answer(
            "Не смог сделать анализ. Проверь GROQ_API_KEY и модель, а сама мысль уже сохранена без изменений."
        )
        return
    await db.update_idea_analysis(callback.from_user.id, idea_id, payload)
    updated = await db.get_idea(callback.from_user.id, idea_id)
    if updated:
        await edit_or_send_idea(callback, updated)


@router.callback_query(F.data.startswith("idea:continue:"))
async def continue_idea(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    idea_id = int(callback.data.split(":")[2])
    row = await db.get_idea(callback.from_user.id, idea_id)
    if not row:
        await callback.answer("Мысль не найдена", show_alert=True)
        return
    await state.update_data(continue_idea_id=idea_id)
    await state.set_state(Form.continue_idea)
    await callback.message.answer(
        f"Продолжаем мысль #{idea_id}: {hbold(html.escape(row['title']))}\n"
        "Напиши новый контекст, решение или следующий шаг. Я обновлю карточку."
    )
    await callback.answer()


@router.message(Form.continue_idea, F.text)
async def continue_idea_text(message: Message, state: FSMContext, db: Database, ai: IdeaAI) -> None:
    if not await db.mark_message_processed(message.chat.id, message.message_id, message.from_user.id):
        return
    data = await state.get_data()
    idea_id = int(data.get("continue_idea_id") or 0)
    row = await db.get_idea(message.from_user.id, idea_id)
    if not row:
        await state.clear()
        await message.answer("Не нашёл эту мысль. Открой «Продолжить мысль» и выбери её заново.")
        return

    continuation = (message.text or "").strip()
    if not continuation:
        await message.answer("Нужен текст продолжения. Можно написать «отмена», чтобы выйти.")
        return
    if continuation.lower() in {"отмена", "cancel", "/cancel"}:
        await state.clear()
        await message.answer("Ок, продолжение отменил.")
        return

    combined_text = build_continuation_text(row["original_text"], continuation)
    status_message = await message.answer("Обновляю карточку с новым контекстом...")
    try:
        payload = await ai.structure_idea(
            combined_text,
            row["source_type"] or "text",
            bool(row["photo_file_id"]),
        )
    except Exception:
        logger.exception("Idea continuation analysis failed")
        payload = ai.raw_idea_payload(combined_text, row["category"])

    await db.update_idea_analysis(message.from_user.id, idea_id, payload, original_text=combined_text)
    if payload.get("category"):
        await db.update_idea_category(message.from_user.id, idea_id, str(payload["category"]))
    await state.clear()
    await delete_status_message(status_message)

    updated = await db.get_idea(message.from_user.id, idea_id)
    await message.answer("Готово, мысль обновлена.")
    if updated:
        await send_idea(message, updated)


@router.message(Form.continue_idea)
async def continue_idea_unsupported(message: Message) -> None:
    await message.answer("Для продолжения этой мысли пришли текстом новый контекст или напиши «отмена».")


@router.callback_query(F.data.startswith("idea:category:"))
async def choose_idea_category(callback: CallbackQuery, state: FSMContext) -> None:
    idea_id = int(callback.data.split(":")[2])
    await state.update_data(category_idea_id=idea_id)
    await state.set_state(Form.set_category)
    await callback.message.answer("Напиши название категории для этой мысли.")
    await callback.answer()


@router.message(Form.set_category, F.text)
async def set_idea_category_text(message: Message, state: FSMContext, db: Database) -> None:
    data = await state.get_data()
    category = category_name_from_chat_text(message.text)
    if not category:
        await message.answer("Нужно название категории текстом.")
        return
    idea_id = int(data["category_idea_id"])
    await db.update_idea_category(message.from_user.id, idea_id, category)
    await state.clear()
    await message.answer(f"Категория обновлена: {html.escape(category)}.")


@router.callback_query(F.data.startswith("idea:pin:"))
async def pin_idea(callback: CallbackQuery, bot: Bot, db: Database) -> None:
    idea_id = int(callback.data.split(":")[2])
    row = await db.get_idea(callback.from_user.id, idea_id)
    if not row:
        await callback.answer("Мысль не найдена", show_alert=True)
        return
    if not callback.message:
        await callback.answer("Не вижу сообщение для закрепления", show_alert=True)
        return
    try:
        await bot.pin_chat_message(
            chat_id=callback.message.chat.id,
            message_id=callback.message.message_id,
            disable_notification=True,
        )
    except TelegramBadRequest as exc:
        logger.warning("Could not pin idea message: %s", exc)
        await callback.answer(
            "Не смог закрепить. В группе боту нужны права на закрепление сообщений.",
            show_alert=True,
        )
        return
    await db.pin_idea(callback.from_user.id, idea_id, callback.message.chat.id, callback.message.message_id)
    row = await db.get_idea(callback.from_user.id, idea_id)
    if row:
        await edit_or_send_idea(callback, row)
    await callback.answer("Закреплено")


@router.callback_query(F.data.startswith("idea:delete:"))
async def delete_idea(callback: CallbackQuery, db: Database) -> None:
    idea_id = int(callback.data.split(":")[2])
    await db.delete_idea(callback.from_user.id, idea_id)
    await callback.message.edit_text("Идея удалена.")
    await callback.answer()


@router.callback_query(F.data.startswith("idea:rename:"))
async def rename_idea(callback: CallbackQuery, state: FSMContext) -> None:
    idea_id = int(callback.data.split(":")[2])
    await state.update_data(rename_idea_id=idea_id)
    await state.set_state(Form.rename_title)
    await callback.message.answer("Напиши новый заголовок.")
    await callback.answer()


@router.message(Form.rename_title, F.text)
async def rename_idea_text(message: Message, state: FSMContext, db: Database) -> None:
    data = await state.get_data()
    title = (message.text or "").strip()
    if not title:
        await message.answer("Заголовок не должен быть пустым.")
        return
    await db.update_title(message.from_user.id, int(data["rename_idea_id"]), title)
    await state.clear()
    await message.answer("Заголовок обновлён.")


@router.callback_query(F.data == "admin:menu")
async def admin_menu_cb(callback: CallbackQuery, db: Database) -> None:
    if not await db.is_admin(callback.from_user.id):
        await callback.answer("Только админ", show_alert=True)
        return
    await callback.message.edit_text("Админка пользователей:", reply_markup=admin_menu())
    await callback.answer()


@router.callback_query(F.data == "admin:users")
async def admin_users(callback: CallbackQuery, db: Database) -> None:
    if not await db.is_admin(callback.from_user.id):
        await callback.answer("Только админ", show_alert=True)
        return
    rows = await db.list_admin_users()
    await send_chunks(callback.message, format_admin_users(rows))
    await callback.answer()


@router.callback_query(F.data == "admin:list")
async def admin_list(callback: CallbackQuery, db: Database) -> None:
    if not await db.is_admin(callback.from_user.id):
        await callback.answer("Только админ", show_alert=True)
        return
    rows = await db.list_allowed()
    lines = [hbold("Разрешённые пользователи")]
    for row in rows:
        role = "admin" if row["is_admin"] else "user"
        username = f" @{row['username']}" if row["username"] else ""
        lines.append(f"- {hcode(str(row['telegram_id']))}{username} ({role})")
    await callback.message.answer("\n".join(lines))
    await callback.answer()


@router.callback_query(F.data == "admin:blocklist")
async def admin_blocklist(callback: CallbackQuery, db: Database) -> None:
    if not await db.is_admin(callback.from_user.id):
        await callback.answer("Только админ", show_alert=True)
        return
    rows = await db.list_blocked()
    await send_chunks(callback.message, format_blocked_users(rows))
    await callback.answer()


@router.callback_query(F.data == "admin:add")
async def admin_add(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    if not await db.is_admin(callback.from_user.id):
        await callback.answer("Только админ", show_alert=True)
        return
    await state.set_state(Form.add_allowed)
    await callback.message.answer("Напиши Telegram ID пользователя. Админские права задаются только в переменной ADMIN_TELEGRAM_IDS.")
    await callback.answer()


@router.message(Form.add_allowed, F.text)
async def admin_add_text(message: Message, state: FSMContext, db: Database, config: Config) -> None:
    if not await db.is_admin(message.from_user.id):
        return
    parts = (message.text or "").split()
    try:
        telegram_id = int(parts[0])
    except (IndexError, ValueError):
        await message.answer("Нужен числовой Telegram ID.")
        return
    is_admin = telegram_id in config.admin_ids
    await db.add_allowed(telegram_id, None, is_admin, message.from_user.id)
    await db.unblock_user(telegram_id)
    await state.clear()
    await message.answer("Пользователь добавлен в whitelist и убран из blacklist, если был там.")


@router.callback_query(F.data == "admin:remove")
async def admin_remove(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    if not await db.is_admin(callback.from_user.id):
        await callback.answer("Только админ", show_alert=True)
        return
    await state.set_state(Form.remove_allowed)
    await callback.message.answer("Напиши Telegram ID, которого нужно убрать.")
    await callback.answer()


@router.message(Form.remove_allowed, F.text)
async def admin_remove_text(message: Message, state: FSMContext, db: Database, config: Config) -> None:
    if not await db.is_admin(message.from_user.id):
        return
    try:
        telegram_id = int((message.text or "").strip())
    except ValueError:
        await message.answer("Нужен числовой Telegram ID.")
        return
    if telegram_id in config.admin_ids:
        await message.answer("Этого админа нельзя удалить через бот. Админские права задаются в ADMIN_TELEGRAM_IDS.")
        await state.clear()
        return
    await db.remove_allowed(telegram_id)
    await state.clear()
    await message.answer("Пользователь удалён из whitelist.")


@router.callback_query(F.data == "admin:block")
async def admin_block(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    if not await db.is_admin(callback.from_user.id):
        await callback.answer("Только админ", show_alert=True)
        return
    await state.set_state(Form.block_user)
    await callback.message.answer("Напиши Telegram ID для blacklist. После ID можно добавить причину: 123456 спам.")
    await callback.answer()


@router.message(Form.block_user, F.text)
async def admin_block_text(message: Message, state: FSMContext, db: Database, config: Config) -> None:
    if not await db.is_admin(message.from_user.id):
        return
    try:
        telegram_id, reason = parse_admin_user_input(message.text)
    except ValueError:
        await message.answer("Нужен числовой Telegram ID. Можно так: 123456 спам.")
        return
    if telegram_id == message.from_user.id or telegram_id in config.admin_ids or await db.is_admin(telegram_id):
        await message.answer("Админа нельзя добавить в blacklist.")
        await state.clear()
        return
    await db.block_user(telegram_id, None, reason, message.from_user.id)
    await state.clear()
    await message.answer("Пользователь добавлен в blacklist. Теперь он не сможет пользоваться ботом.")


@router.callback_query(F.data == "admin:unblock")
async def admin_unblock(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    if not await db.is_admin(callback.from_user.id):
        await callback.answer("Только админ", show_alert=True)
        return
    await state.set_state(Form.unblock_user)
    await callback.message.answer("Напиши Telegram ID, которого нужно убрать из blacklist.")
    await callback.answer()


@router.message(Form.unblock_user, F.text)
async def admin_unblock_text(message: Message, state: FSMContext, db: Database) -> None:
    if not await db.is_admin(message.from_user.id):
        return
    try:
        telegram_id, _ = parse_admin_user_input(message.text)
    except ValueError:
        await message.answer("Нужен числовой Telegram ID.")
        return
    await db.unblock_user(telegram_id)
    await state.clear()
    await message.answer("Пользователь убран из blacklist.")


@router.message(F.text | F.voice | F.audio | F.photo)
async def capture_idea(message: Message, state: FSMContext, bot: Bot, db: Database, ai: IdeaAI, config: Config) -> None:
    await register_seen_user(message, db, config)
    if not await db.mark_message_processed(message.chat.id, message.message_id, message.from_user.id):
        return
    await state.clear()

    source_type = "text"
    photo_file_id = None
    photo_path = None
    photo_ocr_text = None
    photo_ai_text = None
    raw_text = (message.text or message.caption or "").strip()
    category_hint: str | None = None

    if message.photo:
        source_type = "photo"
        photo_caption = raw_text
        status_message = await message.answer("Сохраняю фото и разбираю, что на нём...")
        photo_file_id, photo_path, photo_ocr_text = await save_photo_and_ocr(message, bot, config)
        if photo_path:
            photo_ai_text = await ai.describe_photo(Path(photo_path), photo_caption, photo_ocr_text)
        raw_text = merge_photo_text(photo_caption, photo_ocr_text, photo_ai_text)
        await delete_status_message(status_message)

    if message.voice or message.audio:
        source_type = "voice" if message.voice else "audio"
        if not ai.can_transcribe():
            detail = ai.local_whisper_runtime_issue() if ai.uses_local_whisper_transcriber() else None
            if detail:
                await message.answer(f"Голосовые сейчас не готовы: {detail}.")
            else:
                await message.answer("Голосовые сейчас не настроены. Проверь VOICE_TRANSCRIBER=faster_whisper.")
            return
        media = message.voice or message.audio
        logger.info(
            "Voice input received: user_id=%s chat_id=%s message_id=%s source=%s duration=%s file_size=%s",
            message.from_user.id,
            message.chat.id,
            message.message_id,
            source_type,
            getattr(media, "duration", None),
            getattr(media, "file_size", None),
        )
        status_message = await message.answer("Слушаю и разбираю идею...")
        if ai.voice_is_busy():
            logger.info("Local voice transcriber is busy, this message will wait: message_id=%s", message.message_id)
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            try:
                tg_file = await bot.get_file(media.file_id)
                if not tg_file.file_path:
                    raise RuntimeError("Telegram returned an empty file path")
                suffix = Path(tg_file.file_path).suffix or ".ogg"
            except Exception:
                logger.exception("Failed to get Telegram voice file metadata")
                await edit_status_message(
                    status_message,
                    "Не смог получить голосовой файл от Telegram. Попробуй отправить его ещё раз.",
                    message,
                )
                return
            path = tmp_path / f"voice_input{suffix}"
            try:
                await bot.download_file(tg_file.file_path, destination=path)
                logger.info(
                    "Voice file downloaded: message_id=%s path_suffix=%s bytes=%s",
                    message.message_id,
                    suffix,
                    path.stat().st_size if path.exists() else None,
                )
            except Exception:
                logger.exception("Failed to download Telegram voice file")
                await edit_status_message(
                    status_message,
                    "Не смог скачать голосовой файл от Telegram. Попробуй отправить его ещё раз.",
                    message,
                )
                return
            try:
                raw_text = await asyncio.wait_for(
                    transcribe_with_voice_runtime_lock(message, db, ai, config, path),
                    timeout=config.voice_processing_timeout_seconds + config.voice_lock_wait_seconds,
                )
                logger.info(
                    "Voice transcription succeeded: message_id=%s chars=%s",
                    message.message_id,
                    len(raw_text),
                )
                await delete_status_message(status_message)
            except asyncio.TimeoutError:
                logger.exception("Voice transcription timed out")
                await edit_status_message(
                    status_message,
                    "Распознавание слишком долго обрабатывает это голосовое. Я остановил ожидание, чтобы бот не завис. "
                    "Попробуй отправить голос короче или повтори через минуту.",
                    message,
                )
                return
            except Exception:
                logger.exception("Voice transcription failed")
                await edit_status_message(
                    status_message,
                    "Не смог расшифровать голосовое. "
                    "Проверь настройки VOICE_TRANSCRIBER и попробуй ещё раз.",
                    message,
                )
                return

    raw_text, category_hint = split_category_hint(raw_text)
    if category_hint and not raw_text:
        if await set_latest_idea_category(message, db, category_hint):
            return
        await message.answer("Категорию понял, но пока нет сохранённой мысли, к которой её можно привязать.")
        return

    if not raw_text:
        await message.answer("Не вижу текста идеи. Пришли текст, голосовое, ссылку или фото.")
        return

    if first_url(raw_text):
        if source_type == "text":
            source_type = "link"
        raw_text = await enrich_link_text(raw_text)

    analysis_status = await message.answer("Слушаю тебя и собираю мысль...")
    try:
        if source_type == "photo" and not photo_has_text_context(message.caption or "", photo_ocr_text, photo_ai_text):
            payloads = [ai.photo_without_text_payload(raw_text)]
        else:
            payloads = await ai.structure_entries(raw_text, source_type, bool(photo_file_id))
    except Exception:
        logger.exception("Idea structuring failed")
        payloads = [ai.raw_idea_payload(raw_text, category_hint)]

    if category_hint:
        for payload in payloads:
            payload["category"] = category_hint

    rows = []
    for payload in payloads:
        original_text = str(payload.get("original_text") or raw_text).strip() or raw_text
        idea_id = await db.create_idea(
            message.from_user.id,
            payload,
            original_text,
            source_type,
            photo_file_id,
            photo_path,
            photo_ocr_text,
            photo_ai_text,
        )
        row = await db.get_idea(message.from_user.id, idea_id)
        if row:
            rows.append(row)

    await delete_status_message(analysis_status)
    if len(rows) > 1:
        await message.answer(f"Разобрал на {len(rows)} карточки.")
    for row in rows:
        await send_idea(message, row)


async def send_due_digests(bot: Bot, db: Database) -> None:
    users = await db.all_digest_users()
    now_utc = datetime.now(timezone.utc)
    for user in users:
        try:
            local_now = now_utc.astimezone(ZoneInfo(user.timezone))
        except ZoneInfoNotFoundError:
            local_now = now_utc.astimezone(ZoneInfo("Europe/Moscow"))
        if local_now.weekday() != user.digest_weekday or local_now.strftime("%H:%M") != user.digest_time:
            continue
        digest_key = f"{local_now.date().isoformat()}:{user.digest_time}"
        if await db.has_digest_run(user.telegram_id, digest_key):
            continue
        since = (now_utc - timedelta(days=7)).isoformat()
        rows = await db.ideas_since(user.telegram_id, since)
        try:
            await bot.send_message(user.telegram_id, digest_text(user, rows))
            await db.mark_digest_sent(user.telegram_id, digest_key)
        except Exception:
            logger.exception("Failed to send digest to %s", user.telegram_id)


async def main() -> None:
    config = load_config()
    db = Database(config.database_path)
    await db.connect()
    await db.seed_allowed(config.allowed_ids, config.admin_ids)

    ai = IdeaAI(config)
    logger.info(
        "Voice config: transcriber=%s whisper_model=%s groq_audio_model=%s voice_timeout=%s ffmpeg=%s",
        config.voice_transcriber,
        config.whisper_model,
        config.groq_transcribe_model,
        config.voice_processing_timeout_seconds,
        config.ffmpeg_binary,
    )
    voice_ok, voice_detail = await ai.check_voice_transcriber()
    if voice_ok:
        logger.info("Voice transcriber check succeeded: %s", voice_detail)
    else:
        logger.warning("Voice transcriber check failed: %s", voice_detail)

    bot = Bot(config.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(db=db, config=config, ai=ai)
    dp.message.middleware(AccessMiddleware())
    dp.callback_query.middleware(AccessMiddleware())
    dp.include_router(router)

    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(send_due_digests, "interval", minutes=1, args=[bot, db], id="weekly_digests")
    scheduler.start()

    logger.info("Ideas bot started")
    try:
        base_commands = [
            BotCommand(command="start", description="Первый экран"),
            BotCommand(command="list", description="Мысли"),
            BotCommand(command="next", description="Продолжить мысль"),
            BotCommand(command="search", description="Поиск"),
            BotCommand(command="album", description="Альбом фото"),
            BotCommand(command="archive", description="Архив"),
            BotCommand(command="help", description="Как это работает"),
            BotCommand(command="settings", description="Настройки"),
        ]
        admin_commands = [*base_commands, BotCommand(command="admin", description="Админка")]
        await bot.set_my_commands(base_commands)
        for admin_id in config.admin_ids:
            try:
                await bot.set_my_commands(admin_commands, scope=BotCommandScopeChat(chat_id=admin_id))
            except TelegramBadRequest as exc:
                logger.info("Could not set admin command scope for %s: %s", admin_id, exc)
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    finally:
        scheduler.shutdown(wait=False)
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
