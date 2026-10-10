"""
Telegram-бот: главное меню (aiogram 3.x + aiosqlite)

Установка:
    pip install -U aiogram aiosqlite

Запуск:
    export BOT_TOKEN="123:ABC"
    export SUPPORT_USERNAME="your_support"     # без @
    python main.py
"""

import asyncio
import html
import logging
import os
from datetime import datetime

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandObject, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

# ══════════════════════════════════════════════════════════════
#  НАСТРОЙКИ
# ══════════════════════════════════════════════════════════════
BOT_TOKEN = os.getenv("BOT_TOKEN", "8712603440:AAF7bO-ED3SB_sZV1w2T3ZEnkAZ52iWqSJ8")
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "support")
SHOP_NAME = os.getenv("SHOP_NAME", "XYLI SHOP")
DB_PATH = os.getenv("DB_PATH", "bot.db")
REF_PERCENT = float(os.getenv("REF_PERCENT", "10"))  # % от пополнений реферала
CURRENCY = "$"

# Статусы: (минимум покупок, название)
STATUSES = [
    (0, "🌱 Новичок"),
    (5, "⭐ Покупатель"),
    (25, "🔥 Постоянный клиент"),
    (100, "💎 VIP"),
]

# Кастомные эмодзи (в <tg-emoji> внутри — запасной обычный эмодзи)
EMOJI_PROFILE = '<tg-emoji emoji-id="5452085950022707790">😎</tg-emoji>'
EMOJI_SHOP = '<tg-emoji emoji-id="5199874732983353088">💫</tg-emoji>'
EMOJI_FINANCE = '<tg-emoji emoji-id="5402186569006210455">💱</tg-emoji>'

router = Router()
SEP = "━━━━━━━━━━━━━━━━━━━━"


# ══════════════════════════════════════════════════════════════
#  БАЗА ДАННЫХ
# ══════════════════════════════════════════════════════════════
async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id      INTEGER PRIMARY KEY,
                username     TEXT,
                full_name    TEXT,
                balance      REAL    NOT NULL DEFAULT 0,
                purchased    INTEGER NOT NULL DEFAULT 0,
                spent        REAL    NOT NULL DEFAULT 0,
                deposited    REAL    NOT NULL DEFAULT 0,
                referrer_id  INTEGER,
                ref_earned   REAL    NOT NULL DEFAULT 0,
                reg_date     TEXT    NOT NULL
            )
            """
        )
        await db.commit()


async def get_user(user_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def create_user(user_id: int, username: str | None, full_name: str, referrer_id: int | None) -> bool:
    """Возвращает True, если пользователь новый."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT 1 FROM users WHERE user_id = ?", (user_id,)) as cur:
            if await cur.fetchone():
                return False
        await db.execute(
            "INSERT INTO users (user_id, username, full_name, referrer_id, reg_date) VALUES (?, ?, ?, ?, ?)",
            (user_id, username, full_name, referrer_id, datetime.now().strftime("%d.%m.%Y")),
        )
        await db.commit()
        return True


async def update_profile(user_id: int, username: str | None, full_name: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET username = ?, full_name = ? WHERE user_id = ?",
            (username, full_name, user_id),
        )
        await db.commit()


async def count_referrals(user_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM users WHERE referrer_id = ?", (user_id,)) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


async def add_balance(user_id: int, amount: float) -> None:
    """
    Вызывайте после успешной оплаты пополнения.
    Зачисляет сумму и начисляет реферальный бонус пригласившему.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET balance = balance + ?, deposited = deposited + ? WHERE user_id = ?",
            (amount, amount, user_id),
        )
        async with db.execute("SELECT referrer_id FROM users WHERE user_id = ?", (user_id,)) as cur:
            row = await cur.fetchone()
        if row and row[0]:
            bonus = round(amount * REF_PERCENT / 100, 2)
            await db.execute(
                "UPDATE users SET balance = balance + ?, ref_earned = ref_earned + ? WHERE user_id = ?",
                (bonus, bonus, row[0]),
            )
        await db.commit()


# ══════════════════════════════════════════════════════════════
#  ВСПОМОГАТЕЛЬНОЕ
# ══════════════════════════════════════════════════════════════
def money(value: float) -> str:
    return f"{CURRENCY}{value:,.2f}"


def get_status(purchased: int) -> tuple[str, int | None, int]:
    """(название, порог следующего статуса или None, порог текущего)"""
    current = STATUSES[0]
    nxt = None
    for i, (threshold, name) in enumerate(STATUSES):
        if purchased >= threshold:
            current = (threshold, name)
            nxt = STATUSES[i + 1][0] if i + 1 < len(STATUSES) else None
    return current[1], nxt, current[0]


def progress_bar(purchased: int, start: int, target: int | None, size: int = 10) -> str:
    if target is None:
        return "▰" * size + " MAX"
    span = max(target - start, 1)
    filled = min(size, int((purchased - start) / span * size))
    return "▰" * filled + "▱" * (size - filled) + f" {purchased}/{target}"


def display_name(user: dict) -> str:
    if user.get("username"):
        return f"@{html.escape(user['username'])}"
    return html.escape(user.get("full_name") or "Без имени")


async def build_menu_text(user: dict) -> str:
    status, _, _ = get_status(user["purchased"])

    return (
        f"{EMOJI_SHOP} <b>{html.escape(SHOP_NAME)}</b>\n"
        f"<i>Быстро  •  Надёжно  •  Автоматически</i>\n"
        f"{SEP}\n\n"
        f"{EMOJI_PROFILE} <b>Профиль</b>\n"
        f"├ Никнейм: <b>{display_name(user)}</b>\n"
        f"├ ID: <code>{user['user_id']}</code>\n"
        f"├ Статус: <b>{status}</b>\n"
        f"└ Регистрация: <code>{user['reg_date']}</code>\n\n"
        f"{EMOJI_FINANCE} <b>Финансы</b>\n"
        f"├ Баланс: <b>{money(user['balance'])}</b>\n"
        f"├ Пополнено: <code>{money(user['deposited'])}</code>\n"

        f"<i>Выберите нужный раздел ниже 👇</i>"
    )


# ══════════════════════════════════════════════════════════════
#  КЛАВИАТУРЫ
# ══════════════════════════════════════════════════════════════
def btn(text: str, style: str = "primary", **kwargs) -> InlineKeyboardButton:
    """Кнопка со стилем (Bot API 9.4): primary = синяя, success = зелёная, danger = красная."""
    return InlineKeyboardButton(text=text, style=style, **kwargs)


def main_menu_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(btn(text="Купить", style="success", callback_data="buy", icon_custom_emoji_id="4990307318513009602"))
    kb.row(
        btn(text="Рефералы", callback_data="refs", icon_custom_emoji_id="4960891456869893259"),
        btn(text="Финансы", callback_data="finance", icon_custom_emoji_id="5417924076503062111"),
    )
    kb.row(
        btn(text="Инструкция", callback_data="guide", icon_custom_emoji_id="5366421375605040850"),
        btn(text="Тех поддержка", callback_data="support", icon_custom_emoji_id="5238025132177369293"),
    )
    return kb.as_markup()


def back_kb(*extra: InlineKeyboardButton) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for button in extra:
        kb.row(button)
    kb.row(btn(text="⬅️ Главное меню", callback_data="menu"))
    return kb.as_markup()


async def safe_edit(call: CallbackQuery, text: str, kb: InlineKeyboardMarkup) -> None:
    try:
        await call.message.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise


async def ensure_user(call_or_msg) -> dict:
    u = call_or_msg.from_user
    await create_user(u.id, u.username, u.full_name, None)
    await update_profile(u.id, u.username, u.full_name)
    return await get_user(u.id)


# ══════════════════════════════════════════════════════════════
#  ХЕНДЛЕРЫ
# ══════════════════════════════════════════════════════════════
@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject, bot: Bot) -> None:
    u = message.from_user

    referrer_id = None
    if command.args and command.args.startswith("ref_"):
        raw = command.args[4:]
        if raw.isdigit() and int(raw) != u.id and await get_user(int(raw)):
            referrer_id = int(raw)

    is_new = await create_user(u.id, u.username, u.full_name, referrer_id)
    await update_profile(u.id, u.username, u.full_name)

    if is_new and referrer_id:
        try:
            await bot.send_message(
                referrer_id,
                f"🎉 <b>Новый реферал!</b>\nПо вашей ссылке зарегистрировался "
                f"<b>{html.escape(u.full_name)}</b>.\n"
                f"Вы будете получать <b>{REF_PERCENT:g}%</b> с его пополнений.",
            )
        except Exception:
            pass

    user = await get_user(u.id)
    await message.answer(await build_menu_text(user), reply_markup=main_menu_kb())


@router.callback_query(F.data == "menu")
async def cb_menu(call: CallbackQuery) -> None:
    user = await ensure_user(call)
    await safe_edit(call, await build_menu_text(user), main_menu_kb())
    await call.answer()


@router.callback_query(F.data == "buy")
async def cb_buy(call: CallbackQuery) -> None:
    text = (
        f"<b>🛍 Каталог</b>\n{SEP}\n\n"
        "Здесь будет список ваших товаров и услуг.\n\n"
        "<blockquote>Подключите каталог: выведите категории кнопками, "
        "а при покупке вызывайте списание баланса и увеличивайте счётчик «Куплено».</blockquote>"
    )
    await safe_edit(call, text, back_kb())
    await call.answer()


@router.callback_query(F.data == "refs")
async def cb_refs(call: CallbackQuery, bot: Bot) -> None:
    user = await ensure_user(call)
    me = await bot.get_me()
    link = f"https://t.me/{me.username}?start=ref_{user['user_id']}"
    refs = await count_referrals(user["user_id"])

    text = (
        f"<b>👥 Реферальная программа</b>\n{SEP}\n\n"
        f"Приглашайте друзей и получайте <b>{REF_PERCENT:g}%</b> "
        f"с каждого их пополнения — навсегда.\n\n"
        f"📊 <b>Ваша статистика</b>\n"
        f"├ Приглашено: <b>{refs}</b>\n"
        f"└ Заработано: <b>{money(user['ref_earned'])}</b>\n\n"
        f"🔗 <b>Ваша ссылка</b>\n<code>{link}</code>"
    )
    share = btn(
        text="📤 Поделиться ссылкой",
        url=f"https://t.me/share/url?url={link}&text=Заходи в {SHOP_NAME}!",
    )
    await safe_edit(call, text, back_kb(share))
    await call.answer()


@router.callback_query(F.data == "finance")
async def cb_finance(call: CallbackQuery) -> None:
    user = await ensure_user(call)
    text = (
        f"<b>💳 Финансы</b>\n{SEP}\n\n"
        f"💰 Баланс: <b>{money(user['balance'])}</b>\n\n"
        f"├ Всего пополнено: <code>{money(user['deposited'])}</code>\n"
        f"├ Всего потрачено: <code>{money(user['spent'])}</code>\n"
        f"└ Реферальный доход: <code>{money(user['ref_earned'])}</code>\n\n"
        "<i>Выберите способ пополнения ниже.</i>"
    )
    kb = InlineKeyboardBuilder()
    kb.row(btn(text="➕ Пополнить баланс", callback_data="topup"))
    kb.row(btn(text="⬅️ Главное меню", callback_data="menu"))
    await safe_edit(call, text, kb.as_markup())
    await call.answer()


@router.callback_query(F.data == "topup")
async def cb_topup(call: CallbackQuery) -> None:
    text = (
        f"<b>➕ Пополнение баланса</b>\n{SEP}\n\n"
        "<blockquote>Подключите платёжную систему (CryptoBot, Telegram Stars и т.д.). "
        "После успешной оплаты вызовите <code>add_balance(user_id, amount)</code> — "
        "баланс и реферальный бонус начислятся автоматически.</blockquote>"
    )
    kb = InlineKeyboardBuilder()
    kb.row(btn(text="⬅️ Назад", callback_data="finance"))
    await safe_edit(call, text, kb.as_markup())
    await call.answer()


@router.callback_query(F.data == "guide")
async def cb_guide(call: CallbackQuery) -> None:
    text = (
        f"<b>🔖 Инструкция</b>\n{SEP}\n\n"
        "<b>1️⃣  Пополните баланс</b>\n"
        "Раздел «💳 Финансы» → «Пополнить баланс».\n\n"
        "<b>2️⃣  Выберите товар</b>\n"
        "Раздел «🛍 Купить» → нужная категория.\n\n"
        "<b>3️⃣  Получите заказ</b>\n"
        "Товар выдаётся автоматически сразу после оплаты.\n\n"
        "<b>4️⃣  Зарабатывайте</b>\n"
        f"Приглашайте друзей в «👥 Рефералы» и получайте {REF_PERCENT:g}% с их пополнений.\n\n"
        "<i>Остались вопросы? Напишите в тех поддержку.</i>"
    )
    await safe_edit(call, text, back_kb())
    await call.answer()


@router.callback_query(F.data == "support")
async def cb_support(call: CallbackQuery) -> None:
    text = (
        f"<b>🆘 Техническая поддержка</b>\n{SEP}\n\n"
        "Возникла проблема или есть вопрос? Мы на связи и поможем.\n\n"
        "🕒 Время работы: <b>ежедневно, 10:00 – 23:00</b>\n"
        "⚡ Среднее время ответа: <b>до 15 минут</b>\n\n"
        "<blockquote>Для быстрого решения укажите свой ID: "
        f"<code>{call.from_user.id}</code> и опишите проблему.</blockquote>"
    )
    contact = btn(text="💬 Написать в поддержку", url=f"https://t.me/{SUPPORT_USERNAME}")
    await safe_edit(call, text, back_kb(contact))
    await call.answer()


# ══════════════════════════════════════════════════════════════
#  ЗАПУСК
# ══════════════════════════════════════════════════════════════
async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    await init_db()

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)

    await bot.delete_webhook(drop_pending_updates=True)
    logging.info("Бот запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
