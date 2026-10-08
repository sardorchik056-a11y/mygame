"""Раздел «Шахты»: кирки, шахтёры разных типов и ручной запуск добычи.

Модуль самостоятельный и не импортирует main.py (чтобы не было циклического
импорта). Нужные функции бот передаёт один раз через mine.setup(...).

Как это работает:
  * Шахту нужно запускать вручную. Один запуск длится RUN_HOURS часов,
    добыча идёт только пока шахта работает (в том числе когда игрока нет в боте).
  * Остановить шахту можно только когда с момента запуска прошло больше
    MIN_STOP_MINUTES минут. Всё, что успели добыть, остаётся на складе.
  * Кирка усиливает сразу всех шахтёров (множитель дохода).
  * Шахтёры бывают разных типов: у каждого свой доход, цена и лимит.
    Сильные типы открываются вместе с определёнными кирками.
  * У каждой кирки и у каждого типа шахтёров есть своя карточка характеристик.
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

RUN_HOURS = 8          # сколько длится один запуск шахты
MIN_STOP_MINUTES = 5   # раньше этого срока остановить шахту нельзя

# Кирки идут по порядку. Первая выдаётся бесплатно.
PICKAXES = [
    {
        "name": "Деревянная кирка", "mult": 1.0, "price": 0, "icon": "🪵",
        "desc": "Простая кирка для начала. Работает, пока не сломается терпение.",
    },
    {
        "name": "Каменная кирка", "mult": 1.5, "price": 500, "icon": "🪨",
        "desc": "Тяжёлая и надёжная. Открывает рудокопов.",
    },
    {
        "name": "Железная кирка", "mult": 2.5, "price": 2_500, "icon": "⚙️",
        "desc": "Кованая сталь, острая кромка. Открывает подрывников.",
    },
    {
        "name": "Золотая кирка", "mult": 4.0, "price": 10_000, "icon": "🥇",
        "desc": "Мягкая, но очень дорогая. Зато шахтёры работают как заведённые. "
                "Открывает геологов.",
    },
    {
        "name": "Алмазная кирка", "mult": 7.0, "price": 40_000, "icon": "💎",
        "desc": "Режет любую породу. Открывает гномов-мастеров.",
    },
    {
        "name": "Мифриловая кирка", "mult": 12.0, "price": 150_000, "icon": "✨",
        "desc": "Лёгкая как пёрышко и крепче всего на свете. Открывает бригадиров.",
    },
]

# Типы шахтёров: доход в час (с деревянной киркой), цена первого найма,
# лимит именно этого типа и номер кирки, с которой тип становится доступен.
MINERS = [
    {
        "name": "Новичок", "icon": "👷", "income": 10, "price": 100,
        "max": 10, "req_pick": 0,
        "desc": "Только вчера впервые взял в руки кирку. Старается, но медленно.",
    },
    {
        "name": "Рудокоп", "icon": "⛏️", "income": 25, "price": 400,
        "max": 8, "req_pick": 1,
        "desc": "Знает, где копать. Основа любой шахты.",
    },
    {
        "name": "Подрывник", "icon": "🧨", "income": 60, "price": 1_500,
        "max": 6, "req_pick": 2,
        "desc": "Не любит лишних вопросов и тишину. Зато порода сыплется сама.",
    },
    {
        "name": "Геолог", "icon": "🧭", "income": 150, "price": 6_000,
        "max": 5, "req_pick": 3,
        "desc": "Видит жилы там, где другие видят камни.",
    },
    {
        "name": "Гном-мастер", "icon": "🧙", "income": 400, "price": 25_000,
        "max": 4, "req_pick": 4,
        "desc": "Копает с рождения. Борода длиннее, чем штрек.",
    },
    {
        "name": "Бригадир", "icon": "👑", "income": 1_000, "price": 100_000,
        "max": 3, "req_pick": 5,
        "desc": "Командует всей сменой, и смена работает в полную силу.",
    },
]

MINER_PRICE_GROWTH = 1.35  # каждый следующий шахтёр одного типа дороже в столько раз

PICK_ICON = "⛏️"
MINER_ICON = "👷"
TIMER_ICON = "⏱️"
STORAGE_ICON = "📦"
LOCK_ICON = "🔒"

# Куда админ может добавить фото через /img
IMG_TARGETS: dict[str, str] = {
    "mine": "Шахты",
    "mine_picks": "Шахты: кирки",
    "mine_miners": "Шахты: шахтёры",
}
for _i, _p in enumerate(PICKAXES):
    IMG_TARGETS[f"mine_pick_{_i}"] = f"Шахты: {_p['name']}"
for _i, _m in enumerate(MINERS):
    IMG_TARGETS[f"mine_miner_{_i}"] = f"Шахты: {_m['name']}"


# ---------- Состояние и расчёты ----------

def get_mine(user: dict) -> dict:
    """Данные шахты игрока; при первом входе создаются стартовые.
    Старые данные (miners было числом) переносятся в новый формат."""
    m = user.get("mine")
    changed = False

    if m is None:
        m = user["mine"] = {
            "pick": 0,
            "miners": {"0": 1},   # один новичок бесплатно
            "stored": 0.0,
            "ts": time.time(),
            "run_start": None,
            "run_end": None,
        }
        changed = True

    if isinstance(m.get("miners"), int):  # миграция со старой версии
        m["miners"] = {"0": m["miners"]}
        changed = True
    for key, default in (("run_start", None), ("run_end", None)):
        if key not in m:
            m[key] = default
            changed = True
    m.pop("miners_old", None)

    if changed:
        D.save_users()
    return m


def owned(m: dict, i: int) -> int:
    return int(m["miners"].get(str(i), 0))


def total_miners(m: dict) -> int:
    return sum(int(v) for v in m["miners"].values())


def base_income(m: dict) -> float:
    """Доход всех шахтёров в час без учёта кирки."""
    return sum(owned(m, i) * t["income"] for i, t in enumerate(MINERS))


def income_per_hour(m: dict) -> float:
    return base_income(m) * PICKAXES[m["pick"]]["mult"]


def is_running(m: dict) -> bool:
    return m.get("run_end") is not None


def settle(m: dict) -> None:
    """Зачислить на склад добычу за время, пока шахта работала."""
    now = time.time()
    if is_running(m):
        upto = min(now, m["run_end"])
        hours = max(0.0, upto - m["ts"]) / 3600
        m["stored"] += income_per_hour(m) * hours
        if now >= m["run_end"]:
            m["run_start"] = None
            m["run_end"] = None
    m["ts"] = now


def miner_price(m: dict, i: int) -> int:
    return round(MINERS[i]["price"] * MINER_PRICE_GROWTH ** owned(m, i))


def fmt(n: float) -> str:
    return f"{int(n):,}".replace(",", " ")


def fmt_mult(mult: float) -> str:
    return f"×{mult:g}"


def fmt_dur(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, rest = divmod(seconds, 3600)
    mins, secs = divmod(rest, 60)
    if hours:
        return f"{hours} ч {mins} мин"
    if mins:
        return f"{mins} мин {secs} сек"
    return f"{secs} сек"


def stop_wait_left(m: dict) -> float:
    """Сколько секунд ещё нельзя останавливать шахту (0, если уже можно)."""
    if not is_running(m):
        return 0
    return max(0.0, m["run_start"] + MIN_STOP_MINUTES * 60 - time.time())


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


def _button(data: str, label: str, emoji: str = "", style: str = "success") -> str:
    """Кнопка прямо в теле rich-сообщения."""
    text = f"{emoji} {label}".strip()
    return (
        "<tg-button-row>"
        f'<tg-button type="callback_data" data="{data}" '
        f'style="{style}">{text}</tg-button>'
        "</tg-button-row>"
    )


def _coin() -> str:
    return D.custom_emoji(D.COIN_EMOJI)


def _info() -> str:
    return D.custom_emoji(D.INFO_EMOJI)


def _check() -> str:
    return D.custom_emoji(D.CHECK_EMOJI)


def _image(with_image: bool) -> str:
    return '<img src="tg://photo?id=pet"/>' if with_image else ""


# ---------- Экраны ----------

def mine_html(user_id: int, with_image: bool = False) -> str:
    user, m = load(user_id)
    pick = PICKAXES[m["pick"]]

    if is_running(m):
        left = m["run_end"] - time.time()
        state = f"Работает, до конца {fmt_dur(left)}"
        wait = stop_wait_left(m)
        if wait > 0:
            status = f"Остановить шахту можно через {fmt_dur(wait)}."
        else:
            status = "Шахту можно остановить, добытое останется на складе."
        action = _button("mine:stop", "Остановить шахту", TIMER_ICON, "danger")
    else:
        state = "Стоит"
        status = (
            f"Шахтёры работают только пока шахта запущена. "
            f"Один запуск длится {RUN_HOURS} ч."
        )
        action = _button("mine:start", f"Запустить на {RUN_HOURS} ч", TIMER_ICON)

    return (
        f"{_image(with_image)}"
        f"<p><b>{PICK_ICON} ШАХТЫ</b></p>"
        "<blockquote><i>Запусти шахту, и шахтёры будут добывать монеты, "
        "даже пока тебя нет рядом.</i></blockquote>"
        + _table(
            [
                (f"{TIMER_ICON} Шахта", state),
                (f"{pick['icon']} Кирка", f"{pick['name']} {fmt_mult(pick['mult'])}"),
                (f"{MINER_ICON} Шахтёры", str(total_miners(m))),
                (f"{_coin()} Доход", f"{fmt(income_per_hour(m))} в час"),
                (f"{STORAGE_ICON} Склад", fmt(m["stored"])),
            ]
        )
        + f"<p><b>{_info()} {status}</b></p>"
        + action
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
        rows.append((f"{pick['icon']} {pick['name']} {fmt_mult(pick['mult'])}", price))

    return (
        f"{_image(with_image)}"
        f"<p><b>{PICK_ICON} КИРКИ</b></p>"
        "<blockquote><i>Кирка усиливает сразу всех шахтёров. Покупай по порядку. "
        "Нажми на кирку внизу, чтобы открыть её карточку.</i></blockquote>"
        + _table(rows, head=("Кирка", "Цена"))
    )


def pick_card_html(user_id: int, i: int, with_image: bool = False) -> str:
    user, m = load(user_id)
    pick = PICKAXES[i]
    base = base_income(m)
    now_income = income_per_hour(m)
    new_income = base * pick["mult"]

    if i < m["pick"]:
        status, button = f"{_check()} Куплена", ""
        info = "Эта кирка у тебя уже есть, но сейчас в руках кирка получше."
    elif i == m["pick"]:
        status, button = f"{_check()} В руках", ""
        info = "Именно эта кирка сейчас усиливает всех шахтёров."
    elif i == m["pick"] + 1:
        status = f"{_coin()} Можно купить"
        button = _button(f"mine:buy_pick:{i}", "Купить кирку", pick["icon"])
        info = f"Доход вырастет на {fmt(new_income - now_income)} в час."
    else:
        status, button = f"{LOCK_ICON} Закрыта", ""
        info = f"Сначала купи «{PICKAXES[i - 1]['name']}»."

    opens = [t for t in MINERS if t["req_pick"] == i]
    rows = [
        (f"{pick['icon']} Кирка", pick["name"]),
        (f"{PICK_ICON} Множитель", fmt_mult(pick["mult"])),
        (f"{_coin()} Цена", fmt(pick["price"]) if pick["price"] else "бесплатно"),
        (f"{_coin()} Доход сейчас с ней", f"{fmt(new_income)} в час"),
        ("Статус", status),
    ]
    if opens:
        rows.append(
            (f"{MINER_ICON} Открывает", ", ".join(t["name"] for t in opens))
        )

    return (
        f"{_image(with_image)}"
        f"<p><b>{pick['icon']} {pick['name'].upper()}</b></p>"
        f"<blockquote><i>{pick['desc']}</i></blockquote>"
        + _table(rows)
        + f"<p><b>{_info()} {info}</b></p>"
        + button
    )


def miners_html(user_id: int, with_image: bool = False) -> str:
    user, m = load(user_id)

    rows = []
    for i, t in enumerate(MINERS):
        if m["pick"] >= t["req_pick"]:
            value = f"{owned(m, i)}/{t['max']}"
        else:
            value = f"{LOCK_ICON} {PICKAXES[t['req_pick']]['name']}"
        rows.append((f"{t['icon']} {t['name']}", value))

    return (
        f"{_image(with_image)}"
        f"<p><b>{MINER_ICON} ШАХТЁРЫ</b></p>"
        "<blockquote><i>У каждого типа шахтёров свой доход, цена и лимит. "
        "Нажми на шахтёра внизу, чтобы открыть его карточку.</i></blockquote>"
        + _table(rows, head=("Шахтёр", "Нанято"))
        + f"<p><b>{_info()} Всего шахтёров: {total_miners(m)}. "
        f"Доход шахты: {fmt(income_per_hour(m))} в час.</b></p>"
    )


def miner_card_html(user_id: int, i: int, with_image: bool = False) -> str:
    user, m = load(user_id)
    t = MINERS[i]
    mult = PICKAXES[m["pick"]]["mult"]
    have = owned(m, i)
    unlocked = m["pick"] >= t["req_pick"]

    rows = [
        (f"{t['icon']} Тип", t["name"]),
        (f"{MINER_ICON} Нанято", f"{have}/{t['max']}"),
        (f"{_coin()} Доход одного", f"{fmt(t['income'] * mult)} в час"),
        (f"{_coin()} Доход всех этого типа", f"{fmt(have * t['income'] * mult)} в час"),
        (f"{PICK_ICON} Нужна кирка", PICKAXES[t["req_pick"]]["name"]),
    ]

    button = ""
    if not unlocked:
        rows.append(("Статус", f"{LOCK_ICON} Закрыт"))
        info = f"Купи «{PICKAXES[t['req_pick']]['name']}», чтобы нанимать таких."
    elif have >= t["max"]:
        rows.append(("Статус", f"{_check()} Лимит"))
        info = "Все места для этого типа заняты. Нанимай других шахтёров."
    else:
        price = miner_price(m, i)
        rows.append((f"{_coin()} Цена найма", fmt(price)))
        info = (
            f"Новый {t['name'].lower()} принесёт ещё {fmt(t['income'] * mult)} монет "
            "в час. Каждый следующий этого типа стоит дороже."
        )
        button = _button(f"mine:buy_miner:{i}", "Нанять", t["icon"])

    return (
        f"{_image(with_image)}"
        f"<p><b>{t['icon']} {t['name'].upper()}</b></p>"
        f"<blockquote><i>{t['desc']}</i></blockquote>"
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


def _pairs(buttons: list[InlineKeyboardButton]) -> list[list[InlineKeyboardButton]]:
    return [buttons[i:i + 2] for i in range(0, len(buttons), 2)]


def picks_kb() -> InlineKeyboardMarkup:
    rows = _pairs(
        [_btn(f"{p['icon']} {p['name']}", f"mine:pick:{i}") for i, p in enumerate(PICKAXES)]
    )
    rows.append([_btn("Назад", "mine:open", D.BACK_EMOJI_ID)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def miners_kb() -> InlineKeyboardMarkup:
    rows = _pairs(
        [_btn(f"{t['icon']} {t['name']}", f"mine:miner:{i}") for i, t in enumerate(MINERS)]
    )
    rows.append([_btn("Назад", "mine:open", D.BACK_EMOJI_ID)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _back_to(data: str):
    def kb() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            inline_keyboard=[[_btn("Назад", data, D.BACK_EMOJI_ID)]]
        )
    return kb


# ---------- Отправка ----------

SCREENS = {
    "mine": (mine_html, mine_kb),
    "mine_picks": (picks_html, picks_kb),
    "mine_miners": (miners_html, miners_kb),
}
for _i in range(len(PICKAXES)):
    SCREENS[f"mine_pick_{_i}"] = (
        lambda uid, wi=False, i=_i: pick_card_html(uid, i, wi),
        _back_to("mine:picks"),
    )
for _i in range(len(MINERS)):
    SCREENS[f"mine_miner_{_i}"] = (
        lambda uid, wi=False, i=_i: miner_card_html(uid, i, wi),
        _back_to("mine:miners"),
    )


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


def _index(callback: CallbackQuery, size: int) -> int | None:
    """Номер из callback_data вида «mine:pick:3»; None, если он неверный."""
    try:
        i = int(callback.data.split(":")[2])
    except (IndexError, ValueError):
        return None
    return i if 0 <= i < size else None


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


@router.callback_query(F.data.regexp(r"^mine:pick:\d+$"))
async def on_pick_card(callback: CallbackQuery):
    if await _require_user(callback) is None:
        return
    i = _index(callback, len(PICKAXES))
    if i is None:
        await callback.answer()
        return
    await callback.answer()
    await _show(callback, f"mine_pick_{i}")


@router.callback_query(F.data.regexp(r"^mine:miner:\d+$"))
async def on_miner_card(callback: CallbackQuery):
    if await _require_user(callback) is None:
        return
    i = _index(callback, len(MINERS))
    if i is None:
        await callback.answer()
        return
    await callback.answer()
    await _show(callback, f"mine_miner_{i}")


@router.callback_query(F.data == "mine:start")
async def on_start(callback: CallbackQuery):
    user = await _require_user(callback)
    if user is None:
        return
    m = get_mine(user)
    settle(m)

    if is_running(m):
        await callback.answer("Шахта уже работает.", show_alert=True)
        return
    if total_miners(m) < 1:
        await callback.answer("В шахте нет ни одного шахтёра.", show_alert=True)
        return

    now = time.time()
    m["run_start"] = now
    m["run_end"] = now + RUN_HOURS * 3600
    m["ts"] = now
    D.save_users()
    await callback.answer(f"Шахта запущена на {RUN_HOURS} ч!")
    await _show(callback, "mine")


@router.callback_query(F.data == "mine:stop")
async def on_stop(callback: CallbackQuery):
    user = await _require_user(callback)
    if user is None:
        return
    m = get_mine(user)
    settle(m)

    if not is_running(m):
        await callback.answer("Шахта уже остановлена.", show_alert=True)
        await _show(callback, "mine")
        return

    wait = stop_wait_left(m)
    if wait > 0:
        await callback.answer(
            f"Рано. Шахту можно остановить только через {fmt_dur(wait)} "
            f"(минимум {MIN_STOP_MINUTES} мин после запуска).",
            show_alert=True,
        )
        return

    m["run_start"] = None
    m["run_end"] = None
    D.save_users()
    await callback.answer("Шахта остановлена. Добыча осталась на складе.")
    await _show(callback, "mine")


@router.callback_query(F.data == "mine:collect")
async def on_collect(callback: CallbackQuery):
    user = await _require_user(callback)
    if user is None:
        return
    m = get_mine(user)
    settle(m)

    gain = int(m["stored"])
    if gain < 1:
        await callback.answer("Пока нечего собирать.", show_alert=True)
        return

    m["stored"] -= gain
    user["coins"] += gain
    D.save_users()
    await callback.answer(f"Собрано монет: {fmt(gain)}")
    await _show(callback, "mine")


@router.callback_query(F.data.regexp(r"^mine:buy_pick:\d+$"))
async def on_buy_pick(callback: CallbackQuery):
    user = await _require_user(callback)
    if user is None:
        return
    i = _index(callback, len(PICKAXES))
    if i is None:
        await callback.answer()
        return

    m = get_mine(user)
    settle(m)  # сначала фиксируем добычу по старому доходу

    if i <= m["pick"]:
        await callback.answer("Эта кирка у тебя уже есть.", show_alert=True)
        return
    if i != m["pick"] + 1:
        await callback.answer("Кирки покупаются по порядку.", show_alert=True)
        return

    pick = PICKAXES[i]
    if user["coins"] < pick["price"]:
        await callback.answer(
            f"Не хватает монет: {fmt(pick['price'] - user['coins'])}", show_alert=True
        )
        return

    user["coins"] -= pick["price"]
    m["pick"] = i
    D.save_users()
    await callback.answer(f"Куплено: {pick['name']}!", show_alert=True)
    await _show(callback, f"mine_pick_{i}")


@router.callback_query(F.data.regexp(r"^mine:buy_miner:\d+$"))
async def on_buy_miner(callback: CallbackQuery):
    user = await _require_user(callback)
    if user is None:
        return
    i = _index(callback, len(MINERS))
    if i is None:
        await callback.answer()
        return

    m = get_mine(user)
    settle(m)
    t = MINERS[i]

    if m["pick"] < t["req_pick"]:
        await callback.answer(
            f"Нужна кирка: {PICKAXES[t['req_pick']]['name']}.", show_alert=True
        )
        return
    if owned(m, i) >= t["max"]:
        await callback.answer("Лимит этого типа шахтёров достигнут.", show_alert=True)
        return

    price = miner_price(m, i)
    if user["coins"] < price:
        await callback.answer(
            f"Не хватает монет: {fmt(price - user['coins'])}", show_alert=True
        )
        return

    user["coins"] -= price
    m["miners"][str(i)] = owned(m, i) + 1
    D.save_users()
    await callback.answer(f"Нанят: {t['name']}! Теперь их {owned(m, i)}.")
    await _show(callback, f"mine_miner_{i}")
