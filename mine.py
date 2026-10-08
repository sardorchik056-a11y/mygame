"""Раздел «Шахты»: кирки, шахтёры и пассивный доход монет.

Модуль самостоятельный и не импортирует main.py (чтобы не было циклического
импорта). Нужные функции бот передаёт один раз через mine.setup(...).

Как это работает:
  * Каждый шахтёр добывает монеты, даже пока игрока нет в боте.
  * Кирка усиливает сразу всех шахтёров (множитель дохода).
  * Добыча копится на складе, но не больше, чем за STORAGE_HOURS часов:
    забирать монеты нужно вовремя.
  * Кирки покупаются по порядку, шахтёры нанимаются по одному, и каждый
    следующий стоит дороже.
"""

import logging
import time
from types import SimpleNamespace

from aiogram import F, Router
from aiogram.enums import ButtonStyle
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

router = Router()

# Заполняется в setup()
D = SimpleNamespace()


def setup(**deps) -> None:
    """Передаём модулю функции и эмодзи из main.py."""
    for name, value in deps.items():
        setattr(D, name, value)


# ---------- Баланс шахт ----------

# Кирки: название, множитель дохода, цена в монетах. Первая выдаётся бесплатно.
PICKAXES = [
    {"name": "Деревянная кирка", "mult": 1.0, "price": 0},
    {"name": "Каменная кирка", "mult": 1.5, "price": 500},
    {"name": "Железная кирка", "mult": 2.5, "price": 2_500},
    {"name": "Золотая кирка", "mult": 4.0, "price": 10_000},
    {"name": "Алмазная кирка", "mult": 7.0, "price": 40_000},
    {"name": "Мифриловая кирка", "mult": 12.0, "price": 150_000},
]

MINER_BASE_INCOME = 10     # монет в час у одного шахтёра с деревянной киркой
MINER_BASE_PRICE = 100     # цена первого нанимаемого шахтёра
MINER_PRICE_GROWTH = 1.35  # каждый следующий дороже в столько раз
MAX_MINERS = 30
START_MINERS = 1           # один шахтёр у каждого игрока бесплатно
STORAGE_HOURS = 8          # склад вмещает добычу за столько часов

# Эмодзи, пока обычные. Когда будут кастомные, меняем здесь.
PICK_ICON = "⛏️"
MINER_ICON = "👷"
STORAGE_ICON = "📦"
LOCK_ICON = "🔒"

# Куда админ может добавить фото через /img
IMG_TARGETS: dict[str, str] = {
    "mine": "Шахты",
    "mine_picks": "Шахты: кирки",
    "mine_miners": "Шахты: шахтёры",
}


# ---------- Состояние и расчёты ----------

def get_mine(user: dict) -> dict:
    """Данные шахты игрока; при первом входе создаются стартовые."""
    if "mine" not in user:
        user["mine"] = {
            "pick": 0,
            "miners": START_MINERS,
            "stored": 0.0,
            "ts": time.time(),
        }
        D.save_users()
    return user["mine"]


def income_per_hour(m: dict) -> float:
    return m["miners"] * MINER_BASE_INCOME * PICKAXES[m["pick"]]["mult"]


def capacity(m: dict) -> float:
    return income_per_hour(m) * STORAGE_HOURS


def settle(m: dict) -> None:
    """Зачислить на склад добычу за прошедшее время."""
    now = time.time()
    hours = max(0.0, now - m["ts"]) / 3600
    m["stored"] = min(capacity(m), m["stored"] + income_per_hour(m) * hours)
    m["ts"] = now


def miner_price(m: dict) -> int:
    return round(MINER_BASE_PRICE * MINER_PRICE_GROWTH ** (m["miners"] - 1))


def fmt(n: float) -> str:
    return f"{int(n):,}".replace(",", " ")


def fmt_mult(mult: float) -> str:
    return f"×{mult:g}"


def time_to_full(m: dict) -> str:
    left = capacity(m) - m["stored"]
    minutes = int(left / income_per_hour(m) * 60)
    if minutes < 1:
        return "меньше минуты"
    hours, mins = divmod(minutes, 60)
    return f"{hours} ч {mins} мин" if hours else f"{mins} мин"


def load(user_id: int) -> tuple[dict, dict]:
    """(игрок, шахта) с уже зачисленной добычей. Для админа без питомца
    возвращаем пустые данные, чтобы предпросмотр /img работал."""
    user = D.get_user(user_id) or {"coins": 0}
    m = get_mine(user)
    settle(m)
    return user, m


def _table(rows: list[tuple[str, str]], head=("Параметр", "Значение")) -> str:
    body = "".join(
        "<tr>"
        f"<td><b>{label}</b></td>"
        f'<td align="center"><b>{value}</b></td>'
        "</tr>"
        for label, value in rows
    )
    return (
        "<table bordered striped>"
        f"<tr><th><b>{head[0]}</b></th><th><b>{head[1]}</b></th></tr>"
        f"{body}"
        "</table>"
    )


def _button(data: str, label: str, emoji: str = "") -> str:
    """Зелёная кнопка прямо в теле rich-сообщения."""
    text = f"{emoji} {label}".strip()
    return (
        "<tg-button-row>"
        f'<tg-button type="callback_data" data="{data}" '
        f'style="success">{text}</tg-button>'
        "</tg-button-row>"
    )


def _coin() -> str:
    return D.custom_emoji(D.COIN_EMOJI)


def _info() -> str:
    return D.custom_emoji(D.INFO_EMOJI)


def _check() -> str:
    return D.custom_emoji(D.CHECK_EMOJI)


# ---------- Экраны ----------

def mine_html(user_id: int, with_image: bool = False) -> str:
    user, m = load(user_id)
    pick = PICKAXES[m["pick"]]
    cap = capacity(m)

    if m["stored"] >= cap - 1e-6:
        status = "Склад полон. Забери монеты, пока шахтёры не простаивают."
    else:
        status = f"Склад заполнится через {time_to_full(m)}."

    image = '<img src="tg://photo?id=pet"/>' if with_image else ""
    return (
        f"{image}"
        f"<p><b>{PICK_ICON} ШАХТЫ</b></p>"
        "<blockquote><i>Шахтёры добывают монеты, даже пока тебя нет рядом. "
        "Заглядывай почаще и не давай складу переполниться.</i></blockquote>"
        + _table(
            [
                (f"{PICK_ICON} Кирка", f"{pick['name']} {fmt_mult(pick['mult'])}"),
                (f"{MINER_ICON} Шахтёры", f"{m['miners']}/{MAX_MINERS}"),
                (f"{_coin()} Доход", f"{fmt(income_per_hour(m))} в час"),
                (f"{STORAGE_ICON} Склад", f"{fmt(m['stored'])}/{fmt(cap)}"),
            ]
        )
        + f"<p><b>{_info()} {status}</b></p>"
        + _button("mine:collect", "Собрать монеты", _coin())
    )


def picks_html(user_id: int, with_image: bool = False) -> str:
    user, m = load(user_id)

    rows = []
    for i, pick in enumerate(PICKAXES):
        if i < m["pick"]:
            price = _check()
        elif i == m["pick"]:
            price = f"{_check()} в руках"
        elif i == m["pick"] + 1:
            price = f"{_coin()} {fmt(pick['price'])}"
        else:
            price = f"{LOCK_ICON} {fmt(pick['price'])}"
        rows.append((f"{pick['name']} {fmt_mult(pick['mult'])}", price))

    if m["pick"] + 1 < len(PICKAXES):
        nxt = PICKAXES[m["pick"] + 1]
        info = (
            f"Следующая: {nxt['name']} за {_coin()} {fmt(nxt['price'])}. "
            f"Доход вырастет до {fmt(m['miners'] * MINER_BASE_INCOME * nxt['mult'])} в час."
        )
        button = _button("mine:buy_pick", "Купить кирку", PICK_ICON)
    else:
        info = "У тебя лучшая кирка. Дальше расти можно только числом шахтёров."
        button = ""

    image = '<img src="tg://photo?id=pet"/>' if with_image else ""
    return (
        f"{image}"
        f"<p><b>{PICK_ICON} КИРКИ</b></p>"
        "<blockquote><i>Кирка усиливает сразу всех шахтёров. Покупай по порядку: "
        "каждая следующая добывает заметно больше.</i></blockquote>"
        + _table(rows, head=("Кирка", "Цена"))
        + f"<p><b>{_info()} {info}</b></p>"
        + button
    )


def miners_html(user_id: int, with_image: bool = False) -> str:
    user, m = load(user_id)
    pick = PICKAXES[m["pick"]]
    one = MINER_BASE_INCOME * pick["mult"]

    rows = [
        (f"{MINER_ICON} Шахтёры", f"{m['miners']}/{MAX_MINERS}"),
        (f"{_coin()} Один шахтёр", f"{fmt(one)} в час"),
        (f"{_coin()} Все шахтёры", f"{fmt(income_per_hour(m))} в час"),
    ]

    if m["miners"] < MAX_MINERS:
        price = miner_price(m)
        rows.append((f"{_coin()} Цена найма", fmt(price)))
        info = (
            f"Новый шахтёр принесёт ещё {fmt(one)} монет в час. "
            "Каждый следующий стоит дороже."
        )
        button = _button("mine:buy_miner", "Нанять шахтёра", MINER_ICON)
    else:
        info = "Все места в шахте заняты. Улучшай кирки, чтобы добывать больше."
        button = ""

    image = '<img src="tg://photo?id=pet"/>' if with_image else ""
    return (
        f"{image}"
        f"<p><b>{MINER_ICON} ШАХТЁРЫ</b></p>"
        "<blockquote><i>Больше шахтёров, больше монет. Нанимай помощников и "
        "пусть работа кипит днём и ночью.</i></blockquote>"
        + _table(rows)
        + f"<p><b>{_info()} {info}</b></p>"
        + button
    )


# ---------- Клавиатуры (под сообщением) ----------

def _btn(text: str, data: str, emoji_id: str | None = None) -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text=text,
        callback_data=data,
        style=ButtonStyle.PRIMARY,
        icon_custom_emoji_id=emoji_id,
    )


def mine_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_btn(f"{PICK_ICON} Кирки", "mine:picks"), _btn(f"{MINER_ICON} Шахтёры", "mine:miners")],
            [_btn("Назад", "menu:back", D.BACK_EMOJI_ID)],
        ]
    )


def sub_back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[_btn("Назад", "mine:open", D.BACK_EMOJI_ID)]]
    )


# ---------- Отправка ----------

SCREENS = {
    "mine": (mine_html, mine_kb),
    "mine_picks": (picks_html, sub_back_kb),
    "mine_miners": (miners_html, sub_back_kb),
}


async def send_screen(bot, chat_id: int, user_id: int, key: str) -> None:
    html, kb = SCREENS[key]
    await D.send_rich_card(
        bot, chat_id, key, lambda with_image: html(user_id, with_image), kb()
    )


async def send_preview(bot, chat_id: int, user_id: int, key: str) -> None:
    """Предпросмотр для /img: без кнопок под сообщением."""
    html, _ = SCREENS[key]
    await D.send_rich_card(
        bot, chat_id, key, lambda with_image: html(user_id, with_image)
    )


async def _require_user(callback: CallbackQuery) -> dict | None:
    user = D.get_user(callback.from_user.id)
    if user is None:
        await callback.answer("Сначала выбери питомца: /start", show_alert=True)
    return user


async def _show(callback: CallbackQuery, key: str) -> None:
    """Старое сообщение удаляем, присылаем новое."""
    try:
        await callback.message.delete()
    except Exception as e:  # сообщение могло уже пропасть
        logging.warning("Не удалось удалить сообщение: %s", e)
    await send_screen(callback.bot, callback.message.chat.id, callback.from_user.id, key)


# ---------- Хендлеры ----------

@router.callback_query(F.data.in_({"menu:mine", "mine:open"}))
async def on_open(callback: CallbackQuery):
    if await _require_user(callback) is None:
        return
    await callback.answer()
    await _show(callback, "mine")


@router.callback_query(F.data == "mine:picks")
async def on_picks(callback: CallbackQuery):
    if await _require_user(callback) is None:
        return
    await callback.answer()
    await _show(callback, "mine_picks")


@router.callback_query(F.data == "mine:miners")
async def on_miners(callback: CallbackQuery):
    if await _require_user(callback) is None:
        return
    await callback.answer()
    await _show(callback, "mine_miners")


@router.callback_query(F.data == "mine:collect")
async def on_collect(callback: CallbackQuery):
    user = await _require_user(callback)
    if user is None:
        return
    m = get_mine(user)
    settle(m)

    gain = int(m["stored"])
    if gain < 1:
        await callback.answer("Пока нечего собирать, шахтёры ещё трудятся.", show_alert=True)
        return

    m["stored"] -= gain
    user["coins"] += gain
    D.save_users()
    await callback.answer(f"Собрано монет: {fmt(gain)}")
    await _show(callback, "mine")


@router.callback_query(F.data == "mine:buy_pick")
async def on_buy_pick(callback: CallbackQuery):
    user = await _require_user(callback)
    if user is None:
        return
    m = get_mine(user)
    settle(m)  # сначала фиксируем добычу по старому доходу

    if m["pick"] + 1 >= len(PICKAXES):
        await callback.answer("У тебя уже лучшая кирка.", show_alert=True)
        return

    nxt = PICKAXES[m["pick"] + 1]
    if user["coins"] < nxt["price"]:
        await callback.answer(
            f"Не хватает монет: {fmt(nxt['price'] - user['coins'])}", show_alert=True
        )
        return

    user["coins"] -= nxt["price"]
    m["pick"] += 1
    D.save_users()
    await callback.answer(f"Куплено: {nxt['name']}!", show_alert=True)
    await _show(callback, "mine_picks")


@router.callback_query(F.data == "mine:buy_miner")
async def on_buy_miner(callback: CallbackQuery):
    user = await _require_user(callback)
    if user is None:
        return
    m = get_mine(user)
    settle(m)

    if m["miners"] >= MAX_MINERS:
        await callback.answer("Все места в шахте заняты.", show_alert=True)
        return

    price = miner_price(m)
    if user["coins"] < price:
        await callback.answer(
            f"Не хватает монет: {fmt(price - user['coins'])}", show_alert=True
        )
        return

    user["coins"] -= price
    m["miners"] += 1
    D.save_users()
    await callback.answer(f"Нанят новый шахтёр! Теперь их {m['miners']}.")
    await _show(callback, "mine_miners")
