from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from html import escape as quote_html
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiogram.utils.markdown import hbold, hcode, hitalic

from .db import loads


TYPE_EMOJI = {
    "Идея": "💡",
    "Задача": "✅",
    "Напоминание": "⏰",
    "Контент": "📝",
    "Покупка": "🛒",
    "Мысль": "🧠",
    "Инсайт": "✨",
    "Ссылка": "🔗",
    "Наблюдение": "👁",
}


def entry_type(row) -> str:
    return row["entry_type"] or "Мысль"


def entry_type_line(row) -> str:
    item_type = entry_type(row)
    return f"{TYPE_EMOJI.get(item_type, '🧠')} {hbold(item_type)}"


def summary_for_card(row) -> str:
    text = row["summary"] or row["tldr"] or row["full_text"] or row["original_text"] or ""
    text = " ".join(str(text).split())
    if len(text) <= 320:
        return text
    return text[:317].rstrip(" ,.;:") + "..."


def idea_text(row) -> str:
    lines = [
        entry_type_line(row),
        hbold(row["title"]),
    ]
    summary = summary_for_card(row)
    if summary:
        lines.extend(["", f"{hbold('Summary')}: {quote_html(summary)}"])
    if row["next_step"]:
        lines.extend(["", f"{hbold('Следующий шаг')}: {quote_html(row['next_step'])}"])
    return "\n".join(lines)


def idea_details_text(row) -> str:
    tags = ", ".join(f"#{tag}" for tag in loads(row["tags_json"]))
    tasks = loads(row["tasks_json"])
    key_points = loads(row["key_points_json"])
    questions = loads(row["open_questions_json"])

    lines = [
        entry_type_line(row),
        hbold(row["title"]),
        "",
        hbold("Полный текст"),
        quote_html(row["full_text"] or row["original_text"]),
    ]
    if row["summary"] or row["tldr"]:
        lines.extend(["", f"{hbold('Summary')}: {quote_html(row['summary'] or row['tldr'])}"])
    if "photo_ai_text" in row.keys() and row["photo_ai_text"]:
        lines.extend(["", hbold("AI-описание фото"), quote_html(row["photo_ai_text"])])
    if "photo_ocr_text" in row.keys() and row["photo_ocr_text"]:
        lines.extend(["", hbold("Текст с фото"), quote_html(row["photo_ocr_text"])])
    if tasks:
        lines.append("")
        lines.append(hbold("Задачи"))
        lines.extend(f"- {quote_html(str(item))}" for item in tasks)
    if key_points:
        lines.append("")
        lines.append(hbold("Ключевые пункты"))
        lines.extend(f"- {quote_html(str(item))}" for item in key_points)
    if row["next_step"]:
        lines.extend(["", f"{hbold('Следующий шаг')}: {quote_html(row['next_step'])}"])
    if questions:
        lines.append("")
        lines.append(hbold("Открытые вопросы"))
        lines.extend(f"- {quote_html(str(item))}" for item in questions)
    lines.extend(["", f"{hbold('Категория')}: {quote_html(row['category'] or 'Без категории')}"])
    if tags:
        lines.extend(["", quote_html(tags)])
    return "\n".join(lines)


def compact_list(rows) -> str:
    if not rows:
        return "Пока мыслей нет."
    lines = []
    for row in rows:
        summary = summary_for_card(row)
        lines.append(f"#{row['id']} {TYPE_EMOJI.get(entry_type(row), '🧠')} {quote_html(row['title'])}\n{hitalic(summary)}")
    return "\n\n".join(lines)


def next_steps_text(rows) -> str:
    if not rows:
        return "Пока нет мыслей, которые хочется продолжить."
    lines = [
        hbold("Продолжить мысль"),
        "Выбери мысль ниже: можно открыть подробности или нажать «Продолжить» и дописать новый контекст.",
    ]
    return "\n".join(lines)


def next_step_item_text(row) -> str:
    lines = [
        f"#{row['id']} {TYPE_EMOJI.get(entry_type(row), '🧠')} {hbold(row['title'])}",
    ]
    if row["next_step"]:
        lines.extend(["", f"{hbold('Следующий шаг')}: {quote_html(row['next_step'])}"])
    tasks = loads(row["tasks_json"])
    visible_tasks = []
    for task in tasks:
        task_text = str(task).strip()
        if task_text and task_text != row["next_step"]:
            visible_tasks.append(task_text)
    if visible_tasks:
        lines.append("")
        lines.append(hbold("Задачи"))
        lines.extend(f"- {quote_html(task)}" for task in visible_tasks[:5])
    summary = summary_for_card(row)
    if summary:
        lines.extend(["", quote_html(summary[:500])])
    return "\n".join(lines)


def album_caption(row) -> str:
    summary = summary_for_card(row)
    lines = [
        f"🖼 {hbold(row['title'])}",
        f"{hbold('Карточка')}: #{row['id']}",
    ]
    if summary:
        lines.extend(["", quote_html(summary[:500])])
    if "photo_ai_text" in row.keys() and row["photo_ai_text"]:
        photo_text = " ".join(str(row["photo_ai_text"]).split())
        lines.extend(["", f"{hbold('AI-описание')}: {quote_html(photo_text[:250])}"])
    if row["photo_ocr_text"]:
        ocr = " ".join(str(row["photo_ocr_text"]).split())
        lines.extend(["", f"{hbold('Текст с фото')}: {quote_html(ocr[:250])}"])
    return "\n".join(lines)


def album_list_text(rows) -> str:
    if not rows:
        return "Альбом пока пуст."
    lines = [hbold("Альбом")]
    for row in rows:
        lines.append(f"#{row['id']} {quote_html(row['title'])}")
    return "\n".join(lines)


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
        "Как пользоваться ботом:\n\n"
        "1. Просто отправляй сюда любые мысли.\n\n"
        "Это может быть:\n"
        "- короткая заметка\n"
        "- длинный текст\n"
        "- голосовое сообщение\n"
        "- фото\n"
        "- ссылка\n"
        "- пересланное сообщение\n\n"
        "2. Не нужно ничего сортировать.\n\n"
        "Если в одном сообщении несколько идей, бот сам разделит их на отдельные карточки.\n\n"
        "3. AI автоматически делает summary, выделяет задачи, предлагает следующий шаг и определяет категорию.\n\n"
        "4. Все записи сохраняются в «Мысли». Там можно всё просматривать, искать и убирать ненужное в архив.\n\n"
        "Фото попадают в альбом. Там их можно посмотреть и удалить при необходимости.\n\n"
        "Главная идея - быстро выгружать мысли из головы, а не тратить время на организацию.\n\n"
        f"{hcode('/list')} - мысли\n"
        f"{hcode('/search запрос')} - поиск\n"
        f"{hcode('/next')} - продолжить мысль\n"
        f"{hcode('/album')} - альбом фото\n"
        f"{hcode('/archive')} - архив\n"
        f"{hcode('/settings')} - настройки"
    )


def start_text() -> str:
    return (
        f"{hbold('Привет.')}\n\n"
        "Это место для мыслей, задач и идей.\n"
        "Сюда можно быстро скинуть всё, что не хочется держать в голове.\n\n"
        f"{hbold('Что можно отправлять:')}\n"
        "• текст\n"
        "• голосовые\n"
        "• фото\n"
        "• ссылки\n"
        "• пересланные сообщения\n\n"
        f"{hbold('Что я сделаю:')}\n"
        "• разберу мысли\n"
        "• выделю задачи\n"
        "• сделаю короткое summary\n"
        "• предложу следующий шаг\n\n"
        "Можно прислать даже длинное хаотичное сообщение - AI сам разложит его на отдельные идеи."
    )
