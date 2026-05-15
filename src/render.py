from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from html import escape as quote_html
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiogram.utils.markdown import hbold, hcode, hitalic

from .db import loads


def idea_text(row) -> str:
    tags = ", ".join(f"#{tag}" for tag in loads(row["tags_json"]))
    has_analysis = any(
        [
            row["summary"],
            row["tldr"],
            row["full_text"],
            loads(row["key_points_json"]),
            loads(row["open_questions_json"]),
            row["next_step"],
            row["side_thoughts"],
            tags,
        ]
    )
    lines = [
        f"{hbold(row['title'])}",
        f"Категория: {quote_html(row['category'] or 'Без категории')}",
    ]
    if row["pinned_at"]:
        lines.append("Закреплено в чате")
    lines.extend(["", f"{hbold('Мысль')}", quote_html(row["original_text"])])
    if has_analysis:
        lines.extend(["", hbold("Анализ")])
    if row["summary"]:
        lines.append(quote_html(row["summary"]))
    if row["tldr"]:
        lines.extend(["", f"{hbold('TL;DR')}: {quote_html(row['tldr'])}"])
    if row["full_text"]:
        lines.extend(["", quote_html(row["full_text"])])
    key_points = loads(row["key_points_json"])
    if key_points:
        lines.append("")
        lines.append(hbold("Ключевые тезисы"))
        lines.extend(f"- {quote_html(str(item))}" for item in key_points)
    questions = loads(row["open_questions_json"])
    if questions:
        lines.append("")
        lines.append(hbold("Открытые вопросы"))
        lines.extend(f"- {quote_html(str(item))}" for item in questions)
    if row["next_step"]:
        lines.extend(["", f"{hbold('Следующий шаг')}: {quote_html(row['next_step'])}"])
    if row["side_thoughts"]:
        lines.extend(["", f"{hbold('Побочные мысли')}: {quote_html(row['side_thoughts'])}"])
    if tags:
        lines.extend(["", quote_html(tags)])
    return "\n".join(lines)


def compact_list(rows) -> str:
    if not rows:
        return "Идей пока нет."
    lines = []
    for row in rows:
        pinned = " [закреплено]" if row["pinned_at"] else ""
        category = row["category"] or "Без категории"
        lines.append(f"#{row['id']} {quote_html(row['title'])}{pinned}\n{hitalic(quote_html(category))}")
    return "\n\n".join(lines)


def digest_text(user, rows) -> str:
    name = user.display_name or user.username or "друг"
    if not rows:
        return f"{hbold('Еженедельный дайджест')}\n{name}, за неделю новых идей не было."

    grouped = defaultdict(list)
    for row in rows:
        grouped[row["category"] or "Без категории"].append(row)

    lines = [
        hbold("Еженедельный дайджест"),
        f"{quote_html(name)}, вот идеи за неделю.",
        "",
    ]
    for category, ideas in sorted(grouped.items()):
        lines.append(hbold(category))
        for row in ideas:
            short = row["summary"] or row["tldr"] or row["full_text"] or row["original_text"] or ""
            short = " ".join(short.split())[:220]
            pinned = " [закреплено]" if row["pinned_at"] else ""
            lines.append(f"- #{row['id']} {quote_html(row['title'])}{pinned}: {quote_html(short)}")
        lines.append("")

    lines.append(hbold("Вопрос недели"))
    lines.append("Какие идеи готов отбросить, а какие двигать дальше?")
    return "\n".join(lines)


def period_since(period: str, timezone_name: str = "Europe/Moscow") -> str:
    try:
        user_tz = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        user_tz = ZoneInfo("Europe/Moscow")

    now = datetime.now(user_tz)
    if period == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "week":
        start = now - timedelta(days=7)
    else:
        start = now - timedelta(days=30)
    return start.astimezone(timezone.utc).isoformat()


def usage_help() -> str:
    return (
        "Отправьте текст, голосовое, пересланное сообщение или фото с подписью. "
        "Я сохраню мысль почти дословно. Анализ можно запустить отдельной кнопкой под карточкой.\n\n"
        "Категорию можно добавить сразу: «идея про лендинг категория: маркетинг» или следующим сообщением: «категория маркетинг».\n\n"
        f"{hcode('/list')} - последние 10 идей\n"
        f"{hcode('/search запрос')} - поиск\n"
        f"{hcode('/category')} - идеи по категории\n"
        f"{hcode('/today')} {hcode('/week')} {hcode('/month')} - идеи за период\n"
        f"{hcode('/categories')} - категории\n"
        f"{hcode('/settings')} - дайджест и часовой пояс"
    )
