"""Раздел «Шахты»: шахтёры, кирки-предметы и ручной запуск добычи.

Модуль самостоятельный и не импортирует main.py (чтобы не было циклического
импорта). Нужные функции бот передаёт один раз через mine.setup(...).

Как это работает:
  * Шахту нужно запускать вручную. Один запуск длится RUN_HOURS часов,
    добыча идёт только пока шахта работает (в том числе когда игрока нет в боте).
  * Остановить шахту можно только когда с момента запуска прошло больше
    MIN_STOP_MINUTES минут. Всё, что успели добыть, остаётся на складе.
  * Шахтёры бывают 10 уровней: у каждого свой доход, цена и лимит.
  * Кирки это предметы. Их можно покупать сколько угодно и выдавать шахтёрам.
    У кирки одна характеристика, множитель дохода: шахтёр с киркой приносит
    в столько раз больше. Шахтёр без кирки добывает с множителем ×1.
  * У каждого типа шахтёров и у каждой кирки есть своя карточка.
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

# Кирки: множитель дохода шахтёра и цена одной штуки.
PICKAXES = [
    {"name": "Деревянная кирка", "mult": 1.25, "price": 150, "icon": "🪵",
     "desc": "Простая кирка для начала. Лучше, чем голые руки."},
    {"name": "Каменная кирка", "mult": 1.5, "price": 500, "icon": "🪨",
     "desc": "Тяжёлая и надёжная."},
    {"name": "Железная кирка", "mult": 2.5, "price": 2_500, "icon": "⚙️",
     "desc": "Кованая сталь, острая кромка."},
    {"name": "Золотая кирка", "mult": 4.0, "price": 10_000, "icon": "🥇",
     "desc": "Мягкая, но очень дорогая. Зато шахтёры работают как заведённые."},
    {"name": "Алмазная кирка", "mult": 7.0, "price": 40_000, "icon": "💎",
     "desc": "Режет любую породу."},
    {"name": "Мифриловая кирка", "mult": 12.0, "price": 150_000, "icon": "✨",
     "desc": "Лёгкая как пёрышко и крепче всего на свете."},
]

# Типы шахтёров: доход в час без кирки, цена первого найма, лимит этого типа.
MINERS = [
    {"name": "Шахтёр 1 уровня", "icon": "👷", "income": 10, "price": 100, "max": 10,
     "desc": "Только вчера впервые взял в руки кирку. Старается, но медленно."},
    {"name": "Шахтёр 2 уровня", "icon": "⛏️", "income": 25, "price": 400, "max": 8,
     "desc": "Знает, где копать. Основа любой шахты."},
    {"name": "Шахтёр 3 уровня", "icon": "🧨", "income": 60, "price": 1_500, "max": 6,
     "desc": "Не любит лишних вопросов и тишину. Зато порода сыплется сама."},
    {"name": "Шахтёр 4 уровня", "icon": "🧭", "income": 150, "price": 6_000, "max": 5,
     "desc": "Видит жилы там, где другие видят камни."},
    {"name": "Шахтёр 5 уровня", "icon": "🧙", "income": 400, "price": 25_000, "max": 4,
     "desc": "Копает с рождения. Борода длиннее, чем штрек."},
    {"name": "Шахтёр 6 уровня", "icon": "👑", "income": 1_000, "price": 100_000, "max": 3,
     "desc": "Командует всей сменой, и смена работает в полную силу."},
    {"name": "Шахтёр 7 уровня", "icon": "🛠️", "income": 2_500, "price": 400_000, "max": 3,
     "desc": "Собирает собственные механизмы прямо в забое."},
    {"name": "Шахтёр 8 уровня", "icon": "🚀", "income": 6_000, "price": 1_500_000, "max": 2,
     "desc": "Бурит так быстро, что порода не успевает осыпаться."},
    {"name": "Шахтёр 9 уровня", "icon": "🐉", "income": 15_000, "price": 6_000_000, "max": 2,
     "desc": "Говорят, он договорился с драконом, который охраняет жилы."},
    {"name": "Шахтёр 10 уровня", "icon": "🌟", "income": 40_000, "price": 25_000_000, "max": 1,
     "desc": "Легенда шахт. Один такой стоит целой смены."},
]

MINER_PRICE_GROWTH = 1.35  # каждый следующий шахтёр одного типа дороже в столько раз
START_MINER = 0            # тип шахтёра, который выдаётся бесплатно

MINER_ICON = "👷"

# Кастомные эмодзи: (id, запасной обычный эмодзи)
MINE_EMOJI = ("5461047575379466857", "⛏")     # шахта
PICK_EMOJI = ("5197371802136892976", "⛏")     # кирка
INCOME_EMOJI = ("5202018746297767789", "🪙")  # доход
STORAGE_EMOJI = ("5854908544712707500", "📦")  # склад
START_EMOJI = ("5906852613629941703", "🟢")    # кнопка «Запустить»
STOP_EMOJI = ("5907027122446145395", "🔴")     # кнопка «Остановить»

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
#
# m = {
#   "miners": {"<тип>": сколько нанято},
#   "picks":  {"<кирка>": сколько куплено},
#   "equip":  {"<тип>": {"<кирка>": сколько шахтёров этого типа её носят}},
#   "stored": float, "ts": float, "run_start": float|None, "run_end": float|None,
# }

def get_mine(user: dict) -> dict:
    """Данные шахты игрока; при первом входе создаются стартовые.
    Старые форматы (число шахтёров, одна общая кирка) переносятся сами."""
    m = user.get("mine")
    changed = False

    if m is None:
        m = user["mine"] = {
            "miners": {str(START_MINER): 1},
            "picks": {},
            "equip": {},
            "stored": 0.0,
            "ts": time.time(),
            "run_start": None,
            "run_end": None,
        }
        changed = True

    if isinstance(m.get("miners"), int):  # самая старая версия
        m["miners"] = {"0": m["miners"]}
        changed = True

    if "picks" not in m or "equip" not in m:
        m["picks"], m["equip"] = {}, {}
        old = m.get("pick", 0)  # раньше была одна кирка на всех
        if old > 0:
            # Каждому шахтёру выдаём кирку, которая у него была: доход не упадёт
            for t, cnt in m["miners"].items():
                if cnt > 0:
                    m["equip"][t] = {str(old): cnt}
            m["picks"][str(old)] = sum(m["miners"].values())
        changed = True
    if "pick" in m:
        del m["pick"]
        changed = True

    for key in ("run_start", "run_end"):
        if key not in m:
            m[key] = None
            changed = True

    if changed:
        D.save_users()
    return m


def owned(m: dict, i: int) -> int:
    return int(m["miners"].get(str(i), 0))


def total_miners(m: dict) -> int:
    return sum(int(v) for v in m["miners"].values())


def eq_of(m: dict, i: int) -> dict[int, int]:
    """Какие кирки носят шахтёры типа i: {номер кирки: сколько штук}."""
    return {int(p): int(c) for p, c in m["equip"].get(str(i), {}).items() if c > 0}


def equipped_on(m: dict, i: int) -> int:
    return sum(eq_of(m, i).values())


def bare(m: dict, i: int) -> int:
    """Сколько шахтёров типа i без кирки."""
    return owned(m, i) - equipped_on(m, i)


def picks_owned(m: dict, p: int) -> int:
    return int(m["picks"].get(str(p), 0))


def picks_equipped(m: dict, p: int) -> int:
    return sum(eq_of(m, i).get(p, 0) for i in range(len(MINERS)))


def picks_free(m: dict, p: int) -> int:
    return picks_owned(m, p) - picks_equipped(m, p)


def miner_income(m: dict, i: int) -> float:
    """Доход всех шахтёров типа i в час."""
    mult_sum = bare(m, i) + sum(
        c * PICKAXES[p]["mult"] for p, c in eq_of(m, i).items()
    )
    return MINERS[i]["income"] * mult_sum


def income_per_hour(m: dict) -> float:
    return sum(miner_income(m, i) for i in range(len(MINERS)))


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


def _mine() -> str:
    return D.custom_emoji(MINE_EMOJI)


def _pick() -> str:
    return D.custom_emoji(PICK_EMOJI)


def _income() -> str:
    return D.custom_emoji(INCOME_EMOJI)


def _storage() -> str:
    return D.custom_emoji(STORAGE_EMOJI)


def _image(with_image: bool) -> str:
    return '<img src="tg://photo?id=pet"/>' if with_image else ""


# ---------- Экраны ----------

def mine_html(user_id: int, with_image: bool = False) -> str:
    user, m = load(user_id)
    total_picks = sum(picks_owned(m, p) for p in range(len(PICKAXES)))
    on_miners = sum(equipped_on(m, i) for i in range(len(MINERS)))

    if is_running(m):
        left = m["run_end"] - time.time()
        state = f"Работает, до конца {fmt_dur(left)}"
        wait = stop_wait_left(m)
        if wait > 0:
            status = f"Остановить шахту можно через {fmt_dur(wait)}."
        else:
            status = "Шахту можно остановить, добытое останется на складе."
        action = _button("mine:stop", "Остановить шахту", D.custom_emoji(STOP_EMOJI), "danger")
    else:
        state = "Стоит"
        status = (
            "Шахтёры работают только пока шахта запущена. "
            f"Один запуск длится {RUN_HOURS} ч."
        )
        action = _button("mine:start", f"Запустить на {RUN_HOURS} ч", D.custom_emoji(START_EMOJI))

    return (
        f"{_image(with_image)}"
        f"<p><b>{_mine()} ШАХТЫ</b></p>"
        "<blockquote><i>Запусти шахту, и шахтёры будут добывать монеты, "
        "даже пока тебя нет рядом.</i></blockquote>"
        + _table(
            [
                (f"{_mine()} Шахта", state),
                (f"{MINER_ICON} Шахтёры", str(total_miners(m))),
                (f"{_pick()} Кирки", f"надето {on_miners} из {total_picks}"),
                (f"{_income()} Доход", f"{fmt(income_per_hour(m))} в час"),
                (f"{_storage()} Склад", fmt(m["stored"])),
            ]
        )
        + f"<p><b>{_info()} {status}</b></p>"
        + action
        + _button("mine:collect", "Собрать монеты", _coin())
    )


def picks_html(user_id: int, with_image: bool = False) -> str:
    user, m = load(user_id)
    rows = [
        (
            f"{p['icon']} {p['name']} {fmt_mult(p['mult'])}",
            f"{fmt(p['price'])} · есть {picks_owned(m, i)}",
        )
        for i, p in enumerate(PICKAXES)
    ]
    return (
        f"{_image(with_image)}"
        f"<p><b>{_pick()} КИРКИ</b></p>"
        "<blockquote><i>Кирки покупаются сколько угодно и выдаются шахтёрам "
        "в их карточках. Шахтёр с киркой добывает больше. "
        "Нажми на кирку внизу, чтобы открыть её карточку.</i></blockquote>"
        + _table(rows, head=("Кирка", "Цена · в наличии"))
    )


def pick_card_html(user_id: int, i: int, with_image: bool = False) -> str:
    user, m = load(user_id)
    p = PICKAXES[i]
    rows = [
        (f"{_pick()} Множитель", fmt_mult(p["mult"])),
        (f"{_coin()} Цена", fmt(p["price"])),
        ("Куплено", str(picks_owned(m, i))),
        ("Надето на шахтёрах", str(picks_equipped(m, i))),
        ("Свободно", str(picks_free(m, i))),
    ]
    return (
        f"{_image(with_image)}"
        f"<p><b>{p['icon']} {p['name'].upper()}</b></p>"
        f"<blockquote><i>{p['desc']}</i></blockquote>"
        + _table(rows)
        + f"<p><b>{_info()} Шахтёр с этой киркой приносит в {fmt_mult(p['mult'])[1:]} "
        "раза больше, чем без неё. Выдай её в карточке нужного шахтёра.</b></p>"
        + _button(f"mine:buy_pick:{i}:1", "Купить 1", p["icon"])
        + _button(f"mine:buy_pick:{i}:5", "Купить 5", p["icon"])
    )


def miners_html(user_id: int, with_image: bool = False) -> str:
    user, m = load(user_id)
    rows = [
        (f"{t['icon']} {t['name']}", f"{owned(m, i)}/{t['max']}")
        for i, t in enumerate(MINERS)
    ]
    return (
        f"{_image(with_image)}"
        f"<p><b>{MINER_ICON} ШАХТЁРЫ</b></p>"
        "<blockquote><i>У каждого уровня шахтёров свой доход, цена и лимит. "
        "Нажми на шахтёра внизу, чтобы открыть его карточку, нанять его "
        "и выдать кирки.</i></blockquote>"
        + _table(rows, head=("Шахтёр", "Нанято"))
        + f"<p><b>{_info()} Всего шахтёров: {total_miners(m)}. "
        f"Доход шахты: {fmt(income_per_hour(m))} в час.</b></p>"
    )


def miner_card_html(user_id: int, i: int, with_image: bool = False) -> str:
    user, m = load(user_id)
    t = MINERS[i]
    have = owned(m, i)

    eq = eq_of(m, i)
    eq_text = ", ".join(
        f"{PICKAXES[p]['icon']} {c}" for p, c in sorted(eq.items())
    ) or "нет"

    rows = [
        (f"{t['icon']} Шахтёр", t["name"]),
        (f"{MINER_ICON} Нанято", f"{have}/{t['max']}"),
        (f"{_income()} Доход без кирки", f"{fmt(t['income'])} в час"),
        (f"{_pick()} Без кирки", str(bare(m, i))),
        (f"{_pick()} С кирками", eq_text),
        (f"{_income()} Доход всех этого уровня", f"{fmt(miner_income(m, i))} в час"),
    ]

    button = ""
    if have >= t["max"]:
        info = "Все места для этого уровня заняты. Нанимай шахтёров других уровней."
    else:
        rows.append((f"{_coin()} Цена найма", fmt(miner_price(m, i))))
        info = (
            f"Новый {t['name'].lower()} принесёт ещё {fmt(t['income'])} монет в час "
            "без кирки. Каждый следующий этого уровня стоит дороже."
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


def give_html(user_id: int, i: int, with_image: bool = False) -> str:
    user, m = load(user_id)
    t = MINERS[i]
    free = [(p, picks_free(m, p)) for p in range(len(PICKAXES)) if picks_free(m, p) > 0]

    if bare(m, i) <= 0:
        info = (
            "У всех шахтёров этого уровня уже есть кирки. "
            "Сними одну, чтобы выдать другую."
            if owned(m, i) else "Сначала найми шахтёра этого уровня."
        )
    elif not free:
        info = "Свободных кирок нет. Купи их в разделе «Кирки»."
    else:
        info = f"Без кирки: {bare(m, i)}. Выбери кирку внизу, она достанется одному шахтёру."

    table = (
        _table(
            [
                (f"{PICKAXES[p]['icon']} {PICKAXES[p]['name']} {fmt_mult(PICKAXES[p]['mult'])}",
                 f"{c} шт.")
                for p, c in free
            ],
            head=("Свободная кирка", "Сколько"),
        )
        if free else ""
    )
    return (
        f"{_image(with_image)}"
        f"<p><b>{t['icon']} ВЫДАТЬ КИРКУ: {t['name'].upper()}</b></p>"
        + table
        + f"<p><b>{_info()} {info}</b></p>"
    )


def take_html(user_id: int, i: int, with_image: bool = False) -> str:
    user, m = load(user_id)
    t = MINERS[i]
    eq = eq_of(m, i)

    table = (
        _table(
            [
                (f"{PICKAXES[p]['icon']} {PICKAXES[p]['name']} {fmt_mult(PICKAXES[p]['mult'])}",
                 f"{c} шт.")
                for p, c in sorted(eq.items())
            ],
            head=("Надетая кирка", "Сколько"),
        )
        if eq else ""
    )
    info = (
        "Выбери кирку внизу, она вернётся на склад кирок."
        if eq else "У шахтёров этого уровня нет кирок."
    )
    return (
        f"{_image(with_image)}"
        f"<p><b>{t['icon']} СНЯТЬ КИРКУ: {t['name'].upper()}</b></p>"
        + table
        + f"<p><b>{_info()} {info}</b></p>"
    )


# ---------- Клавиатуры (под сообщением) ----------

def _btn(text: str, data: str, emoji_id: str | None = None) -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text=text,
        callback_data=data,
        style=ButtonStyle.PRIMARY,
        icon_custom_emoji_id=emoji_id,
    )


def _pairs(buttons: list[InlineKeyboardButton]) -> list[list[InlineKeyboardButton]]:
    return [buttons[i:i + 2] for i in range(0, len(buttons), 2)]


def _back_row(data: str) -> list[InlineKeyboardButton]:
    return [_btn("Назад", data, D.BACK_EMOJI_ID)]


def mine_kb(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_btn("Кирки", "mine:picks", PICK_EMOJI[0]), _btn(f"{MINER_ICON} Шахтёры", "mine:miners")],
            _back_row("menu:back"),
        ]
    )


def picks_kb(user_id: int) -> InlineKeyboardMarkup:
    rows = _pairs([_btn(f"{p['icon']} {p['name']}", f"mine:pick:{i}") for i, p in enumerate(PICKAXES)])
    rows.append(_back_row("mine:open"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def miners_kb(user_id: int) -> InlineKeyboardMarkup:
    rows = _pairs([_btn(f"{t['icon']} {t['name']}", f"mine:miner:{i}") for i, t in enumerate(MINERS)])
    rows.append(_back_row("mine:open"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _back_to(data: str):
    def kb(user_id: int) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[_back_row(data)])
    return kb


def _miner_card_kb(i: int):
    def kb(user_id: int) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [_btn("Выдать кирку", f"mine:give:{i}"), _btn("Снять кирку", f"mine:take:{i}")],
                _back_row("mine:miners"),
            ]
        )
    return kb


def _give_kb(i: int):
    def kb(user_id: int) -> InlineKeyboardMarkup:
        _, m = load(user_id)
        buttons = []
        if bare(m, i) > 0:
            buttons = [
                _btn(f"{PICKAXES[p]['icon']} {fmt_mult(PICKAXES[p]['mult'])} ({picks_free(m, p)})",
                     f"mine:give:{i}:{p}")
                for p in range(len(PICKAXES)) if picks_free(m, p) > 0
            ]
        rows = _pairs(buttons)
        rows.append(_back_row(f"mine:miner:{i}"))
        return InlineKeyboardMarkup(inline_keyboard=rows)
    return kb


def _take_kb(i: int):
    def kb(user_id: int) -> InlineKeyboardMarkup:
        _, m = load(user_id)
        rows = _pairs(
            [
                _btn(f"{PICKAXES[p]['icon']} {fmt_mult(PICKAXES[p]['mult'])} ({c})",
                     f"mine:take:{i}:{p}")
                for p, c in sorted(eq_of(m, i).items())
            ]
        )
        rows.append(_back_row(f"mine:miner:{i}"))
        return InlineKeyboardMarkup(inline_keyboard=rows)
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
        _miner_card_kb(_i),
    )
    SCREENS[f"mine_give_{_i}"] = (
        lambda uid, wi=False, i=_i: give_html(uid, i, wi),
        _give_kb(_i),
    )
    SCREENS[f"mine_take_{_i}"] = (
        lambda uid, wi=False, i=_i: take_html(uid, i, wi),
        _take_kb(_i),
    )


async def send_screen(bot, chat_id: int, user_id: int, key: str) -> None:
    html, kb = SCREENS[key]
    await D.send_rich_card(
        bot, chat_id, key, lambda with_image: html(user_id, with_image), kb(user_id)
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


def _ints(callback: CallbackQuery) -> list[int] | None:
    """Числа из callback_data вида «mine:give:2:1» (всё после второго слова)."""
    try:
        return [int(x) for x in callback.data.split(":")[2:]]
    except ValueError:
        return None


def _valid(callback: CallbackQuery, *sizes: int) -> list[int] | None:
    nums = _ints(callback)
    if nums is None or len(nums) < len(sizes):
        return None
    for n, size in zip(nums, sizes):
        if not 0 <= n < size:
            return None
    return nums


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
    nums = _valid(callback, len(PICKAXES))
    await callback.answer()
    if nums:
        await _show(callback, f"mine_pick_{nums[0]}")


@router.callback_query(F.data.regexp(r"^mine:miner:\d+$"))
async def on_miner_card(callback: CallbackQuery):
    if await _require_user(callback) is None:
        return
    nums = _valid(callback, len(MINERS))
    await callback.answer()
    if nums:
        await _show(callback, f"mine_miner_{nums[0]}")


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


@router.callback_query(F.data.regexp(r"^mine:buy_pick:\d+:\d+$"))
async def on_buy_pick(callback: CallbackQuery):
    user = await _require_user(callback)
    if user is None:
        return
    nums = _ints(callback)
    if not nums or len(nums) != 2 or not 0 <= nums[0] < len(PICKAXES) or not 1 <= nums[1] <= 10:
        await callback.answer()
        return
    i, n = nums

    m = get_mine(user)
    p = PICKAXES[i]
    cost = p["price"] * n
    if user["coins"] < cost:
        await callback.answer(
            f"Не хватает монет: {fmt(cost - user['coins'])}", show_alert=True
        )
        return

    user["coins"] -= cost
    m["picks"][str(i)] = picks_owned(m, i) + n
    D.save_users()
    await callback.answer(f"Куплено: {p['name']} ×{n}. Теперь их {picks_owned(m, i)}.")
    await _show(callback, f"mine_pick_{i}")


@router.callback_query(F.data.regexp(r"^mine:buy_miner:\d+$"))
async def on_buy_miner(callback: CallbackQuery):
    user = await _require_user(callback)
    if user is None:
        return
    nums = _valid(callback, len(MINERS))
    if not nums:
        await callback.answer()
        return
    i = nums[0]

    m = get_mine(user)
    settle(m)
    t = MINERS[i]

    if owned(m, i) >= t["max"]:
        await callback.answer("Лимит шахтёров этого уровня достигнут.", show_alert=True)
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


# --- Выдать / снять кирку ---

@router.callback_query(F.data.regexp(r"^mine:give:\d+$"))
async def on_give_open(callback: CallbackQuery):
    if await _require_user(callback) is None:
        return
    nums = _valid(callback, len(MINERS))
    await callback.answer()
    if nums:
        await _show(callback, f"mine_give_{nums[0]}")


@router.callback_query(F.data.regexp(r"^mine:take:\d+$"))
async def on_take_open(callback: CallbackQuery):
    if await _require_user(callback) is None:
        return
    nums = _valid(callback, len(MINERS))
    await callback.answer()
    if nums:
        await _show(callback, f"mine_take_{nums[0]}")


@router.callback_query(F.data.regexp(r"^mine:give:\d+:\d+$"))
async def on_give(callback: CallbackQuery):
    user = await _require_user(callback)
    if user is None:
        return
    nums = _valid(callback, len(MINERS), len(PICKAXES))
    if not nums:
        await callback.answer()
        return
    i, p = nums

    m = get_mine(user)
    settle(m)  # доход меняется: сначала фиксируем добычу по старому

    if bare(m, i) <= 0:
        await callback.answer(
            "У всех шахтёров этого уровня уже есть кирки. Сними одну.", show_alert=True
        )
        return
    if picks_free(m, p) <= 0:
        await callback.answer("Свободных кирок такого вида нет.", show_alert=True)
        return

    slot = m["equip"].setdefault(str(i), {})
    slot[str(p)] = int(slot.get(str(p), 0)) + 1
    D.save_users()
    await callback.answer(f"{MINERS[i]['name']} получил: {PICKAXES[p]['name']}")
    await _show(callback, f"mine_give_{i}")


@router.callback_query(F.data.regexp(r"^mine:take:\d+:\d+$"))
async def on_take(callback: CallbackQuery):
    user = await _require_user(callback)
    if user is None:
        return
    nums = _valid(callback, len(MINERS), len(PICKAXES))
    if not nums:
        await callback.answer()
        return
    i, p = nums

    m = get_mine(user)
    settle(m)

    slot = m["equip"].get(str(i), {})
    if int(slot.get(str(p), 0)) <= 0:
        await callback.answer("Такой кирки у этих шахтёров нет.", show_alert=True)
        return

    slot[str(p)] -= 1
    if slot[str(p)] <= 0:
        del slot[str(p)]
    D.save_users()
    await callback.answer(f"Снято: {PICKAXES[p]['name']}. Она вернулась в запас.")
    await _show(callback, f"mine_take_{i}")
