from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def start_menu(is_admin: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text="➕ Добавить мысль", callback_data="nav:add"),
            InlineKeyboardButton(text="💭 Мысли", callback_data="nav:list"),
        ],
        [InlineKeyboardButton(text="❓ Как это работает", callback_data="nav:how")],
    ]
    if is_admin:
        rows.append([InlineKeyboardButton(text="🛡 Админка", callback_data="admin:menu")])
    return InlineKeyboardMarkup(
        inline_keyboard=rows
    )


def main_menu(is_admin: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text="💭 Мысли", callback_data="nav:list"),
            InlineKeyboardButton(text="✍️ Продолжить мысль", callback_data="nav:steps"),
        ],
        [
            InlineKeyboardButton(text="🔍 Поиск", callback_data="nav:search"),
            InlineKeyboardButton(text="📎 Архив", callback_data="nav:archive"),
        ],
        [
            InlineKeyboardButton(text="🖼 Альбом", callback_data="nav:album"),
        ],
        [
            InlineKeyboardButton(text="⚙ Настройки", callback_data="nav:settings"),
        ],
    ]
    if is_admin:
        rows.append([InlineKeyboardButton(text="🛡 Админка", callback_data="admin:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def idea_actions(idea_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Подробнее", callback_data=f"idea:details:{idea_id}"),
                InlineKeyboardButton(text="Архивировать", callback_data=f"idea:archive:{idea_id}"),
            ],
        ]
    )


def next_step_actions(idea_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Продолжить", callback_data=f"idea:continue:{idea_id}"),
                InlineKeyboardButton(text="Подробнее", callback_data=f"idea:details:{idea_id}"),
            ],
            [InlineKeyboardButton(text="Архивировать", callback_data=f"idea:archive:{idea_id}")],
        ]
    )


def archived_idea_actions(idea_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Подробнее", callback_data=f"idea:details:{idea_id}"),
                InlineKeyboardButton(text="Вернуть", callback_data=f"idea:restore:{idea_id}"),
            ],
        ]
    )


def album_photo_actions(idea_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Подробнее", callback_data=f"idea:details:{idea_id}"),
                InlineKeyboardButton(text="Удалить фото", callback_data=f"photo:delete:{idea_id}"),
            ],
        ]
    )


def periods_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Сегодня", callback_data="period:today"),
                InlineKeyboardButton(text="Неделя", callback_data="period:week"),
                InlineKeyboardButton(text="Месяц", callback_data="period:month"),
            ],
            [InlineKeyboardButton(text="Назад", callback_data="nav:menu")],
        ]
    )


def settings_menu(digest_enabled: bool) -> InlineKeyboardMarkup:
    toggle = "Выключить дайджест" if digest_enabled else "Включить дайджест"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=toggle, callback_data="settings:toggle_digest")],
            [
                InlineKeyboardButton(text="Время", callback_data="settings:time"),
                InlineKeyboardButton(text="Часовой пояс", callback_data="settings:timezone"),
            ],
            [InlineKeyboardButton(text="Назад", callback_data="nav:menu")],
        ]
    )


def categories_menu(categories: list) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=f"{row['name']} ({row['ideas_count']})", callback_data=f"cat:view:{row['id']}")] for row in categories[:20]]
    rows.append([InlineKeyboardButton(text="Добавить категорию", callback_data="cat:add")])
    rows.append([InlineKeyboardButton(text="Назад", callback_data="nav:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Пользователи", callback_data="admin:users"),
                InlineKeyboardButton(text="Blacklist", callback_data="admin:blocklist"),
            ],
            [
                InlineKeyboardButton(text="Whitelist", callback_data="admin:list"),
                InlineKeyboardButton(text="Добавить", callback_data="admin:add"),
            ],
            [
                InlineKeyboardButton(text="Убрать доступ", callback_data="admin:remove"),
                InlineKeyboardButton(text="Заблокировать", callback_data="admin:block"),
            ],
            [
                InlineKeyboardButton(text="Разблокировать", callback_data="admin:unblock"),
                InlineKeyboardButton(text="Назад", callback_data="nav:menu"),
            ],
        ]
    )
