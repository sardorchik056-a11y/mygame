"""
Telegram-бот: главное меню + пополнение через xRocket (aiogram 3.x + aiosqlite)

Установка:
    pip install -U aiogram aiosqlite aiohttp

Запуск:
    export BOT_TOKEN="123:ABC"
    export XROCKET_API_KEY="ключ из @xrocket → Rocket Pay → Create App → API token"
    export SUPPORT_USERNAME="your_support"     # без @
    python main.py
"""

import asyncio
import html
import logging
import os
import time
from datetime import datetime
from urllib.parse import quote

import aiohttp
import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
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

# ── xRocket (оплата) ───────────────────────────────────────────
XROCKET_API_KEY = os.getenv("XROCKET_API_KEY", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJhcHBJZCI6IjMwMDgzMiIsImp0aSI6ImFwcDozMDA4MzI6MGViMmU0YjYtMmY4ZC00MzYwLWFjNWUtZWUzMTkzMTI3MTdjIiwiaWF0IjoxNzkxNjM1MzY5fQ.tuxepw0hfGRPsS42-yYoYrBbVC27PiCTrIiCyVR8aYw")
XROCKET_URL = os.getenv("XROCKET_URL", "https://pay.xrocket.tg")  # для testnet укажите URL из документации xRocket
PAY_CURRENCY = os.getenv("PAY_CURRENCY", "USDT")   # валюта счёта (1 USDT = 1 $)
TOPUP_AMOUNTS = [1, 2, 5, 10, 25, 50, 100, 250, 500]  # кнопки быстрых сумм
MIN_TOPUP = 1.0
MAX_TOPUP = 1000.0
INVOICE_TTL = 3600     # срок жизни счёта, сек
POLL_INTERVAL = 8      # как часто проверять оплату, сек

# Статусы: (минимум покупок, название)
STATUSES = [
    (0, "🌱 Новичок"),
    (5, "⭐ Покупатель"),
    (25, "🔥 Постоянный клиент"),
    (100, "💎 VIP"),
]

# Кастомные эмодзи (в <tg-emoji> внутри — запасной обычный эмодзи)
EMOJI_PROFILE = '<tg-emoji emoji-id="5452085950022707790">😎</tg-emoji>'
NUM_1 = '<tg-emoji emoji-id="5830126888357468979">1️⃣</tg-emoji>'
NUM_2 = '<tg-emoji emoji-id="5830254543375441108">2️⃣</tg-emoji>'
NUM_3 = '<tg-emoji emoji-id="5827786453303696733">3️⃣</tg-emoji>'
NUM_4 = '<tg-emoji emoji-id="5830434773088083875">4️⃣</tg-emoji>'
EMOJI_REFS = '<tg-emoji emoji-id="4960891456869893259">💠</tg-emoji>'
EMOJI_STATS = '<tg-emoji emoji-id="5854798142578368552">📊</tg-emoji>'
EMOJI_LINK = '<tg-emoji emoji-id="5271604874419647061">🔗</tg-emoji>'
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
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS payments (
                invoice_id  TEXT    PRIMARY KEY,
                user_id     INTEGER NOT NULL,
                amount      REAL    NOT NULL,
                status      TEXT    NOT NULL DEFAULT 'pending',
                link        TEXT,
                chat_id     INTEGER,
                message_id  INTEGER,
                created_at  INTEGER NOT NULL,
                expires_at  INTEGER NOT NULL
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


async def _credit(db: aiosqlite.Connection, user_id: int, amount: float) -> None:
    """Зачисляет сумму и реферальный бонус (внутри уже открытой транзакции)."""
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


async def add_balance(user_id: int, amount: float) -> None:
    """Ручное зачисление: сумма + реферальный бонус пригласившему."""
    async with aiosqlite.connect(DB_PATH) as db:
        await _credit(db, user_id, amount)
        await db.commit()


async def create_payment(
    invoice_id: str, user_id: int, amount: float, link: str, chat_id: int, message_id: int
) -> None:
    now = int(time.time())
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO payments (invoice_id, user_id, amount, link, chat_id, message_id, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (invoice_id, user_id, amount, link, chat_id, message_id, now, now + INVOICE_TTL),
        )
        await db.commit()


async def get_payment(invoice_id: str) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM payments WHERE invoice_id = ?", (invoice_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def set_payment_status(invoice_id: str, status: str) -> bool:
    """Меняет статус только у ожидающего платежа. True — если статус сменился."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "UPDATE payments SET status = ? WHERE invoice_id = ? AND status = 'pending'",
            (status, invoice_id),
        )
        await db.commit()
        return cur.rowcount == 1


async def credit_payment(invoice_id: str) -> bool:
    """
    Атомарно: помечает платёж оплаченным и зачисляет баланс.
    Повторный вызов ничего не зачислит (защита от двойного начисления).
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "UPDATE payments SET status = 'paid' WHERE invoice_id = ? AND status = 'pending'",
            (invoice_id,),
        )
        if cur.rowcount != 1:
            await db.rollback()
            return False
        async with db.execute(
            "SELECT user_id, amount FROM payments WHERE invoice_id = ?", (invoice_id,)
        ) as c:
            user_id, amount = await c.fetchone()
        await _credit(db, user_id, amount)
        await db.commit()
        return True


async def pending_payments() -> list[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT invoice_id FROM payments WHERE status = 'pending'") as cur:
            return [r[0] for r in await cur.fetchall()]


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
        f"└ Потрачено: <code>{money(user['spent'])}</code>\n\n"
        f"{SEP}\n"
        f"<i>Выберите нужный раздел ниже 👇</i>"
    )


# ══════════════════════════════════════════════════════════════
#  КЛАВИАТУРЫ
# ══════════════════════════════════════════════════════════════
def btn(text: str, style: str = "primary", **kwargs) -> InlineKeyboardButton:
    """Кнопка со стилем (Bot API 9.4): primary = синяя, success = зелёная, danger = красная."""
    return InlineKeyboardButton(text=text, style=style, **kwargs)


def back_btn(callback_data: str = "menu") -> InlineKeyboardButton:
    """Кнопка «Назад» с кастомным эмодзи."""
    return btn(text="Назад", callback_data=callback_data, icon_custom_emoji_id="5258236805890710909")


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
    kb.row(back_btn("menu"))
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
async def cmd_start(message: Message, command: CommandObject, bot: Bot, state: FSMContext) -> None:
    await state.clear()
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
async def cb_menu(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
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
        f"{EMOJI_REFS} <b>Реферальная программа</b>\n{SEP}\n\n"
        f"<blockquote><i>Приглашайте друзей в {html.escape(SHOP_NAME)} и получайте "
        f"{REF_PERCENT:g}% с каждого их пополнения — без ограничений по времени "
        f"и количеству приглашённых.\n\n"
        f"Бонус зачисляется на ваш баланс автоматически сразу после того, как друг "
        f"пополнит счёт. Просто отправьте ему свою ссылку и наблюдайте, как растёт ваш доход.</i></blockquote>\n\n"
        f"{EMOJI_STATS} <b>Ваша статистика</b>\n"
        f"├ Приглашено: <b>{refs}</b>\n"
        f"└ Заработано: <b>{money(user['ref_earned'])}</b>\n\n"
        f"{EMOJI_LINK} <b>Ваша ссылка</b>\n"
        f"<code>{link}</code>"
    )
    share_text = quote(f"Заходи в {SHOP_NAME}!")
    share = btn(
        text="Поделиться ссылкой",
        style="success",
        icon_custom_emoji_id="5264759912025564026",
        url=f"https://t.me/share/url?url={quote(link, safe='')}&text={share_text}",
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
    kb.row(back_btn("menu"))
    await safe_edit(call, text, kb.as_markup())
    await call.answer()


# ══════════════════════════════════════════════════════════════
#  XROCKET: API
# ══════════════════════════════════════════════════════════════
class XRocketError(Exception):
    pass


async def xr_request(method: str, path: str, payload: dict | None = None) -> dict:
    headers = {"Rocket-Pay-Key": XROCKET_API_KEY, "Content-Type": "application/json"}
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.request(
            method, XROCKET_URL.rstrip("/") + path, json=payload, headers=headers
        ) as resp:
            data = await resp.json(content_type=None)
    if resp.status >= 400 or not isinstance(data, dict) or data.get("success") is False:
        raise XRocketError(f"HTTP {resp.status}: {str(data)[:300]}")
    return data.get("data", data)


async def xr_create_invoice(user_id: int, amount: float) -> dict:
    return await xr_request(
        "POST",
        "/tg-invoices",
        {
            "amount": amount,
            "numPayments": 1,
            "currency": PAY_CURRENCY,
            "description": f"Пополнение баланса {SHOP_NAME}",
            "hiddenMessage": "Спасибо! Баланс пополнится автоматически.",
            "commentsEnabled": False,
            "payload": str(user_id),
            "expiredIn": INVOICE_TTL,
        },
    )


async def xr_get_invoice(invoice_id: str) -> dict:
    return await xr_request("GET", f"/tg-invoices/{invoice_id}")


# ══════════════════════════════════════════════════════════════
#  ПОПОЛНЕНИЕ: ЭКРАНЫ
# ══════════════════════════════════════════════════════════════
class TopUp(StatesGroup):
    amount = State()


async def edit_or_send(bot: Bot, chat_id: int, message_id: int, text: str, kb: InlineKeyboardMarkup) -> None:
    try:
        await bot.edit_message_text(
            text, chat_id=chat_id, message_id=message_id, reply_markup=kb, disable_web_page_preview=True
        )
    except TelegramBadRequest as e:
        if "message is not modified" in str(e):
            return
        try:
            await bot.send_message(chat_id, text, reply_markup=kb, disable_web_page_preview=True)
        except Exception:
            logging.exception("Не удалось отправить сообщение о платеже")


async def topup_screen(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    user = await get_user(user_id)
    balance = user["balance"] if user else 0.0
    text = (
        f"{EMOJI_FINANCE} <b>Пополнение баланса</b>\n{SEP}\n\n"
        "<blockquote><i>Выберите сумму пополнения или введите свою. "
        "Оплата проходит через xRocket в криптовалюте — быстро и безопасно. "
        "Баланс пополнится автоматически сразу после оплаты.</i></blockquote>\n\n"
        f"💳 <b>Способ оплаты:</b> xRocket ({PAY_CURRENCY})\n"
        f"📉 <b>Минимум:</b> {CURRENCY}{MIN_TOPUP:g}\n"
        f"📈 <b>Максимум:</b> {CURRENCY}{MAX_TOPUP:g}\n"
        f"💰 <b>Ваш баланс:</b> {money(balance)}\n\n"
        "<i>Выберите сумму ниже 👇</i>"
    )
    kb = InlineKeyboardBuilder()
    for i in range(0, len(TOPUP_AMOUNTS), 3):
        kb.row(
            *[
                btn(text=f"{CURRENCY}{a:g}", callback_data=f"pay:{a:g}")
                for a in TOPUP_AMOUNTS[i : i + 3]
            ]
        )
    kb.row(btn(text="✏️ Другая сумма", callback_data="pay_custom"))
    kb.row(back_btn("finance"))
    return text, kb.as_markup()


def invoice_screen(invoice_id: str, amount: float, link: str) -> tuple[str, InlineKeyboardMarkup]:
    text = (
        f"🧾 <b>Счёт на оплату</b>\n{SEP}\n\n"
        "<blockquote><i>Нажмите «Оплатить» и завершите платёж в xRocket. "
        "Баланс пополнится автоматически — проверять вручную не обязательно.</i></blockquote>\n\n"
        f"💵 <b>К оплате:</b> {money(amount)} ({PAY_CURRENCY})\n"
        f"🆔 <b>Счёт:</b> <code>#{html.escape(invoice_id)}</code>\n"
        f"⏳ <b>Действует:</b> {INVOICE_TTL // 60} мин.\n"
        "📌 <b>Статус:</b> ожидает оплаты"
    )
    kb = InlineKeyboardBuilder()
    kb.row(btn(text="💳 Оплатить", style="success", url=link))
    kb.row(btn(text="🔄 Проверить оплату", callback_data=f"check:{invoice_id}"))
    kb.row(btn(text="✖️ Отменить счёт", style="danger", callback_data=f"cancel:{invoice_id}"))
    return text, kb.as_markup()


async def show_invoice(bot: Bot, user_id: int, chat_id: int, message_id: int, amount: float) -> None:
    try:
        inv = await xr_create_invoice(user_id, amount)
        invoice_id = str(inv["id"])
        link = inv["link"]
    except Exception:
        logging.exception("xRocket: не удалось создать счёт")
        text = (
            f"⚠️ <b>Не удалось создать счёт</b>\n{SEP}\n\n"
            "<blockquote><i>Платёжная система временно недоступна. "
            "Попробуйте ещё раз через минуту или напишите в тех поддержку.</i></blockquote>"
        )
        kb = InlineKeyboardBuilder()
        kb.row(back_btn("topup"))
        await edit_or_send(bot, chat_id, message_id, text, kb.as_markup())
        return

    await create_payment(invoice_id, user_id, amount, link, chat_id, message_id)
    text, kb = invoice_screen(invoice_id, amount, link)
    await edit_or_send(bot, chat_id, message_id, text, kb)
    start_watcher(bot, invoice_id)


# ══════════════════════════════════════════════════════════════
#  ПОПОЛНЕНИЕ: ФОНОВАЯ ПРОВЕРКА ОПЛАТЫ
# ══════════════════════════════════════════════════════════════
watchers: dict[str, asyncio.Task] = {}


def start_watcher(bot: Bot, invoice_id: str) -> None:
    task = watchers.get(invoice_id)
    if task and not task.done():
        return
    watchers[invoice_id] = asyncio.create_task(watch_invoice(bot, invoice_id))


async def finalize_paid(bot: Bot, invoice_id: str) -> None:
    if not await credit_payment(invoice_id):
        return  # уже зачислено ранее
    pay = await get_payment(invoice_id)
    user = await get_user(pay["user_id"])
    text = (
        f"✅ <b>Оплата прошла успешно</b>\n{SEP}\n\n"
        f"<blockquote><i>Баланс пополнен автоматически. "
        f"Спасибо, что выбираете {html.escape(SHOP_NAME)}!</i></blockquote>\n\n"
        f"💵 <b>Зачислено:</b> {money(pay['amount'])}\n"
        f"💰 <b>Ваш баланс:</b> {money(user['balance'])}\n"
        f"🆔 <b>Счёт:</b> <code>#{html.escape(invoice_id)}</code>"
    )
    kb = InlineKeyboardBuilder()
    kb.row(back_btn("menu"))
    await edit_or_send(bot, pay["chat_id"], pay["message_id"], text, kb.as_markup())


async def finalize_expired(bot: Bot, invoice_id: str) -> None:
    if not await set_payment_status(invoice_id, "expired"):
        return
    pay = await get_payment(invoice_id)
    text = (
        f"⌛ <b>Счёт истёк</b>\n{SEP}\n\n"
        "<blockquote><i>Время на оплату вышло, деньги не списаны. "
        "Создайте новый счёт, чтобы пополнить баланс.</i></blockquote>\n\n"
        f"🆔 <b>Счёт:</b> <code>#{html.escape(invoice_id)}</code>"
    )
    kb = InlineKeyboardBuilder()
    kb.row(back_btn("topup"))
    await edit_or_send(bot, pay["chat_id"], pay["message_id"], text, kb.as_markup())


async def watch_invoice(bot: Bot, invoice_id: str) -> None:
    try:
        while True:
            pay = await get_payment(invoice_id)
            if not pay or pay["status"] != "pending":
                return
            try:
                status = str((await xr_get_invoice(invoice_id)).get("status", "")).lower()
            except Exception:
                logging.warning("xRocket: ошибка проверки счёта %s", invoice_id)
                status = ""
            if status == "paid":
                await finalize_paid(bot, invoice_id)
                return
            if status == "expired" or time.time() > pay["expires_at"] + 120:
                await finalize_expired(bot, invoice_id)
                return
            await asyncio.sleep(POLL_INTERVAL)
    except asyncio.CancelledError:
        raise
    except Exception:
        logging.exception("Ошибка в watch_invoice(%s)", invoice_id)
    finally:
        watchers.pop(invoice_id, None)


# ══════════════════════════════════════════════════════════════
#  ПОПОЛНЕНИЕ: ХЕНДЛЕРЫ
# ══════════════════════════════════════════════════════════════
@router.callback_query(F.data == "topup")
async def cb_topup(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await ensure_user(call)
    text, kb = await topup_screen(call.from_user.id)
    await safe_edit(call, text, kb)
    await call.answer()


@router.callback_query(F.data.startswith("pay:"))
async def cb_pay_amount(call: CallbackQuery, bot: Bot, state: FSMContext) -> None:
    await state.clear()
    await ensure_user(call)
    try:
        amount = round(float(call.data.split(":", 1)[1]), 2)
    except ValueError:
        await call.answer("Неверная сумма", show_alert=True)
        return
    if not MIN_TOPUP <= amount <= MAX_TOPUP:
        await call.answer("Сумма вне допустимого диапазона", show_alert=True)
        return
    await call.answer("Создаю счёт…")
    await show_invoice(bot, call.from_user.id, call.message.chat.id, call.message.message_id, amount)


@router.callback_query(F.data == "pay_custom")
async def cb_pay_custom(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(TopUp.amount)
    await state.update_data(msg_id=call.message.message_id)
    text = (
        f"✏️ <b>Своя сумма</b>\n{SEP}\n\n"
        "<blockquote><i>Отправьте сообщением сумму пополнения в долларах. "
        "Можно использовать дробные значения, например 7.5</i></blockquote>\n\n"
        f"📉 <b>Минимум:</b> {CURRENCY}{MIN_TOPUP:g}\n"
        f"📈 <b>Максимум:</b> {CURRENCY}{MAX_TOPUP:g}"
    )
    kb = InlineKeyboardBuilder()
    kb.row(back_btn("topup"))
    await safe_edit(call, text, kb.as_markup())
    await call.answer()


@router.message(TopUp.amount, F.text)
async def msg_custom_amount(message: Message, bot: Bot, state: FSMContext) -> None:
    raw = message.text.replace("$", "").replace(",", ".").strip()
    try:
        amount = round(float(raw), 2)
    except ValueError:
        amount = None

    if amount is None or not MIN_TOPUP <= amount <= MAX_TOPUP:
        await message.answer(
            f"⚠️ <b>Неверная сумма.</b>\n"
            f"<i>Введите число от {CURRENCY}{MIN_TOPUP:g} до {CURRENCY}{MAX_TOPUP:g}.</i>"
        )
        return

    data = await state.get_data()
    await state.clear()
    try:
        await message.delete()
    except Exception:
        pass
    msg_id = data.get("msg_id")
    if msg_id is None:
        sent = await message.answer("⏳ Создаю счёт…")
        msg_id = sent.message_id
    await show_invoice(bot, message.from_user.id, message.chat.id, msg_id, amount)


async def _own_payment(call: CallbackQuery, invoice_id: str) -> dict | None:
    pay = await get_payment(invoice_id)
    if not pay or pay["user_id"] != call.from_user.id:
        await call.answer("Счёт не найден", show_alert=True)
        return None
    return pay


@router.callback_query(F.data.startswith("check:"))
async def cb_check(call: CallbackQuery, bot: Bot) -> None:
    invoice_id = call.data.split(":", 1)[1]
    pay = await _own_payment(call, invoice_id)
    if not pay:
        return
    if pay["status"] != "pending":
        await call.answer("Этот счёт уже обработан", show_alert=True)
        return
    try:
        status = str((await xr_get_invoice(invoice_id)).get("status", "")).lower()
    except Exception:
        logging.exception("xRocket: ошибка проверки счёта")
        await call.answer("Не удалось проверить оплату, попробуйте позже", show_alert=True)
        return

    if status == "paid":
        await call.answer("✅ Оплата получена!")
        await finalize_paid(bot, invoice_id)
    elif status == "expired":
        await call.answer()
        await finalize_expired(bot, invoice_id)
    else:
        await call.answer("⏳ Оплата пока не поступила. Попробуйте через несколько секунд.", show_alert=True)


@router.callback_query(F.data.startswith("cancel:"))
async def cb_cancel(call: CallbackQuery, bot: Bot) -> None:
    invoice_id = call.data.split(":", 1)[1]
    pay = await _own_payment(call, invoice_id)
    if not pay:
        return

    if pay["status"] == "pending":
        # если деньги уже пришли — не отменяем, а зачисляем
        try:
            status = str((await xr_get_invoice(invoice_id)).get("status", "")).lower()
        except Exception:
            status = ""
        if status == "paid":
            await call.answer("✅ Оплата получена!")
            await finalize_paid(bot, invoice_id)
            return
        try:
            await xr_request("DELETE", f"/tg-invoices/{invoice_id}")
        except Exception:
            logging.warning("xRocket: не удалось удалить счёт %s", invoice_id)
        await set_payment_status(invoice_id, "cancelled")

    text, kb = await topup_screen(call.from_user.id)
    await safe_edit(call, text, kb)
    await call.answer("Счёт отменён")


@router.callback_query(F.data == "guide")
async def cb_guide(call: CallbackQuery) -> None:
    text = (
        f"<b>🔖 Инструкция</b>\n{SEP}\n\n"
        f"{NUM_1} <b>Возьмите номер</b>\n"
        "<i>С баланса замораживается сумма. Вы получаете номер и пароль, если он есть.</i>\n\n"
        f"{NUM_2} <b>Подтвердите отправку</b>\n"
        "<i>Введите номер в MAX и нажмите «✅ Код отправлен». На это даётся 5 минут. "
        "Не успели — заявка сгорит, деньги вернутся.</i>\n\n"
        f"{NUM_3} <b>Ждите SMS</b>\n"
        "<i>Бот пришлёт код и пароль в течение 1 минуты.</i>\n\n"
        f"{NUM_4} <b>Введите код</b>\n"
        "<i>Как только код получен — средства списываются. Услуга оказана.</i>"
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

    if XROCKET_API_KEY.startswith("PASTE_"):
        logging.warning("XROCKET_API_KEY не задан — пополнение работать не будет")
    for invoice_id in await pending_payments():
        start_watcher(bot, invoice_id)  # продолжаем следить за счетами после перезапуска

    await bot.delete_webhook(drop_pending_updates=True)
    logging.info("Бот запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
