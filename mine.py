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

Удобства:
  * прогресс-бар смены, время до конца, прогноз добычи за смену;
  * «Собрать и запустить снова» одной кнопкой;
  * покупка кирок ×1 / ×5 / максимум, найм шахтёров ×1 / максимум;
  * выдача и снятие кирок: по одной или сразу все;
  * «Авто-выдача»: сама раздаёт лучшие кирки лучшим шахтёрам;
  * окупаемость найма и прирост дохода от кирки показаны прямо в карточках;
  * статистика: всего добыто и сколько смен запущено.
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
    {"name": "Miner-lvl1", "income": 10, "price": 100, "max": 10,
     "desc": "Только вчера впервые взял в руки кирку. Старается, но медленно."},
    {"name": "Miner-lvl2", "income": 25, "price": 400, "max": 8,
     "desc": "Знает, где копать. Основа любой шахты."},
    {"name": "Miner-lvl3", "income": 60, "price": 1_500, "max": 6,
     "desc": "Не любит лишних вопросов и тишину. Зато порода сыплется сама."},
    {"name": "Miner-lvl4", "income": 150, "price": 6_000, "max": 5,
     "desc": "Видит жилы там, где другие видят камни."},
    {"name": "Miner-lvl5", "income": 400, "price": 25_000, "max": 4,
     "desc": "Копает с рождения. Борода длиннее, чем штрек."},
    {"name": "Miner-lvl6", "income": 1_000, "price": 100_000, "max": 3,
     "desc": "Командует всей сменой, и смена работает в полную силу."},
    {"name": "Miner-lvl7", "income": 2_500, "price": 400_000, "max": 3,
     "desc": "Собирает собственные механизмы прямо в забое."},
    {"name": "Miner-lvl8", "income": 6_000, "price": 1_500_000, "max": 2,
     "desc": "Бурит так быстро, что порода не успевает осыпаться."},
    {"name": "Miner-lvl9", "income": 15_000, "price": 6_000_000, "max": 2,
     "desc": "Говорят, он договорился с драконом, который охраняет жилы."},
    {"name": "Miner-lvl10", "income": 40_000, "price": 25_000_000, "max": 1,
     "desc": "Легенда шахт. Один такой стоит целой смены."},
]

MINER_PRICE_GROWTH = 1.35  # каждый следующий шахтёр одного типа дороже в столько раз
START_MINER = 0            # тип шахтёра, который выдаётся бесплатно
MAX_BULK = 99              # максимум штук за одну покупку/выдачу


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
    "mine_miners": "Шахты: все шахтёры (общее фото)",
}
for _i, _p in enumerate(PICKAXES):
    IMG_TARGETS[f"mine_pick_{_i}"] = f"Шахты: {_p['name']}"


# ---------- Состояние и расчёты ----------
#
# m = {
#   "miners": {"<тип>": сколько нанято},
#   "picks":  {"<кирка>": сколько куплено},
#   "equip":  {"<тип>": {"<кирка>": сколько шахтёров этого типа её носят}},
#   "stored": float, "ts": float, "run_start": float|None, "run_end": float|None,
#   "total": float (всего добыто за всё время), "runs": int (сколько смен запущено),
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
            "total": 0.0,
            "runs": 0,
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

    for key, default in (("run_start", None), ("run_end", None),
                         ("total", 0.0), ("runs", 0)):
        if key not in m:
            m[key] = default
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
        gain = income_per_hour(m) * hours
        m["stored"] += gain
        m["total"] = m.get("total", 0.0) + gain
        if now >= m["run_end"]:
            m["run_start"] = None
            m["run_end"] = None
    m["ts"] = now


def hire_cost(m: dict, i: int, n: int = 1) -> int:
    """Сколько стоит нанять n шахтёров типа i подряд (с учётом роста цены)."""
    have = owned(m, i)
    n = max(0, min(n, MINERS[i]["max"] - have))
    return sum(
        round(MINERS[i]["price"] * MINER_PRICE_GROWTH ** (have + k)) for k in range(n)
    )


def miner_price(m: dict, i: int) -> int:
    return hire_cost(m, i, 1)


def max_affordable_hire(m: dict, i: int, coins: int) -> int:
    """Сколько шахтёров типа i можно нанять на coins (в пределах лимита)."""
    n = 0
    while n < MINERS[i]["max"] - owned(m, i) and hire_cost(m, i, n + 1) <= coins:
        n += 1
    return n


def auto_equip(m: dict) -> None:
    """Раздать все кирки так, чтобы доход был максимальным:
    лучшие кирки достаются шахтёрам с самым большим базовым доходом."""
    picks: list[int] = []
    for p in sorted(range(len(PICKAXES)), key=lambda p: -PICKAXES[p]["mult"]):
        picks += [p] * picks_owned(m, p)

    equip: dict[str, dict[str, int]] = {}
    k = 0
    for i in sorted(range(len(MINERS)), key=lambda i: -MINERS[i]["income"]):
        for _ in range(owned(m, i)):
            if k >= len(picks):
                break
            slot = equip.setdefault(str(i), {})
            slot[str(picks[k])] = slot.get(str(picks[k]), 0) + 1
            k += 1
    m["equip"] = equip


def best_bare_gain(m: dict, p: int) -> float:
    """Прирост дохода в час, если кирку p надеть на лучшего шахтёра без кирки."""
    for i in sorted(range(len(MINERS)), key=lambda i: -MINERS[i]["income"]):
        if bare(m, i) > 0:
            return MINERS[i]["income"] * (PICKAXES[p]["mult"] - 1)
    return 0.0


def run_progress(m: dict) -> float:
    """Доля пройденной смены от 0 до 1."""
    total = m["run_end"] - m["run_start"]
    if total <= 0:
        return 1.0
    return min(1.0, max(0.0, (time.time() - m["run_start"]) / total))


def fmt(n: float) -> str:
    return f"{int(n):,}".replace(",", " ")


def fmt_rate(n: float) -> str:
    """Доход в час: до 100 показываем десятые, дальше целые."""
    if n >= 100 or abs(n - round(n)) < 0.05:
        return fmt(round(n))
    return f"{n:.1f}".replace(".", ",")


def fmt_mult(mult: float) -> str:
    return f"×{mult:g}"


def fmt_bonus(mult: float) -> str:
    return f"+{round((mult - 1) * 100):g}%"


def fmt_dur(seconds: float) -> str:
    seconds = max(0, int(seconds))
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    mins, secs = divmod(rest, 60)
    if days:
        return f"{days} д {hours} ч"
    if hours:
        return f"{hours} ч {mins} мин"
    if mins:
        return f"{mins} мин {secs} сек"
    return f"{secs} сек"


def bar(frac: float, size: int = 10) -> str:
    filled = round(frac * size)
    return "▰" * filled + "▱" * (size - filled)


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


def _table3(rows: list[tuple[str, str, str]], head: tuple[str, str, str]) -> str:
    body = "".join(
        "<tr>"
        f"<td><b>{a}</b></td>"
        f'<td align="center"><b>{b}</b></td>'
        f'<td align="center"><b>{c}</b></td>'
        "</tr>"
        for a, b, c in rows
    )
    return (
        "<table bordered striped>"
        f"<tr><th><b>{head[0]}</b></th><th><b>{head[1]}</b></th>"
        f"<th><b>{head[2]}</b></th></tr>"
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
    rate = income_per_hour(m)
    stored = int(m["stored"])

    rows: list[tuple[str, str]] = []
    if is_running(m):
        frac = run_progress(m)
        left = m["run_end"] - time.time()
        rows.append((f"{_mine()} Шахта", "Работает"))
        rows.append(("Смена", f"{bar(frac)} {int(frac * 100)}%"))
        rows.append(("До конца смены", fmt_dur(left)))
        wait = stop_wait_left(m)
        if wait > 0:
            status = f"Остановить шахту можно через {fmt_dur(wait)}."
        else:
            status = "Шахту можно остановить, добытое останется на складе."
        action = _button("mine:stop", "Остановить шахту", D.custom_emoji(STOP_EMOJI), "danger")
        extra = ""
    else:
        if stored >= 1:
            rows.append((f"{_mine()} Шахта", "Смена окончена"))
            status = "Смена окончена. Забери добычу и запускай шахту снова."
        else:
            rows.append((f"{_mine()} Шахта", "Стоит"))
            status = (
                "Шахтёры работают только пока шахта запущена. "
                f"Один запуск длится {RUN_HOURS} ч."
            )
        action = _button(
            "mine:start", f"Запустить на {RUN_HOURS} ч", D.custom_emoji(START_EMOJI)
        )
        extra = (
            _button(
                "mine:restart", f"Собрать и запустить на {RUN_HOURS} ч",
                D.custom_emoji(START_EMOJI),
            )
            if stored >= 1 else ""
        )

    rows += [
        ("👷 Шахтёры", str(total_miners(m))),
        (f"{_pick()} Кирки", f"надето {on_miners} из {total_picks}"),
        (f"{_income()} Доход", f"{fmt_rate(rate)} в час"),
        (f"{_income()} За смену ({RUN_HOURS} ч)", fmt(rate * RUN_HOURS)),
        (f"{_storage()} Склад", fmt(m["stored"])),
        (f"{_coin()} Всего добыто", fmt(m.get("total", 0))),
        ("Смен запущено", str(m.get("runs", 0))),
    ]

    return (
        f"{_image(with_image)}"
        f"<p><b>{_mine()} ШАХТЫ</b></p>"
        "<blockquote><i>Запусти шахту, и шахтёры будут добывать монеты, "
        "даже пока тебя нет рядом.</i></blockquote>"
        + _table(rows)
        + f"<p><b>{_info()} {status}</b></p>"
        + action
        + extra
        + _button("mine:collect", f"Собрать монеты ({fmt(stored)})", _coin())
    )


def picks_html(user_id: int, with_image: bool = False) -> str:
    user, m = load(user_id)
    rows = [
        (f"{p['icon']} {p['name']}", f"{picks_owned(m, i)} шт.", f"{picks_equipped(m, i)} шт.")
        for i, p in enumerate(PICKAXES)
    ]
    return (
        f"{_image(with_image)}"
        f"<p><b>{_pick()} КИРКИ</b></p>"
        "<blockquote><i>Кирки покупаются сколько угодно и выдаются шахтёрам "
        "в их карточках. Шахтёр с киркой добывает больше. "
        "Нажми на кирку внизу, чтобы открыть её карточку.</i></blockquote>"
        + _table3(rows, ("Кирка", "Куплено", "Надето"))
        + f"<p><b>{_info()} Кнопка «Авто-выдача» сама раздаёт лучшие кирки "
        "лучшим шахтёрам.</b></p>"
    )


def pick_card_html(user_id: int, i: int, with_image: bool = False) -> str:
    user, m = load(user_id)
    p = PICKAXES[i]
    free = picks_free(m, i)
    gain = best_bare_gain(m, i)

    rows = [
        (f"{_coin()} Цена", fmt(p["price"])),
        (f"{_income()} Множитель дохода", f"{fmt_mult(p['mult'])} ({fmt_bonus(p['mult'])})"),
        ("Куплено", str(picks_owned(m, i))),
        ("Надето на шахтёрах", str(picks_equipped(m, i))),
        ("Свободно", str(free)),
    ]
    if gain > 0:
        rows.append(
            (f"{_income()} Прирост на лучшем шахтёре без кирки", f"+{fmt_rate(gain)} в час")
        )
        info = "Выдай её шахтёру в его карточке или нажми «Авто-выдача»."
    else:
        info = "Свободных шахтёров без кирки нет. Найми новых, чтобы выдать эту кирку."

    coins = user.get("coins", 0)
    buttons = _button(f"mine:buy_pick:{i}:1", f"Купить 1 · {fmt(p['price'])}", p["icon"])
    buttons += _button(f"mine:buy_pick:{i}:5", f"Купить 5 · {fmt(p['price'] * 5)}", p["icon"])
    if coins >= p["price"] * 2:
        buttons += _button(
            f"mine:buy_pick:{i}:0",
            f"Купить максимум ({min(coins // p['price'], MAX_BULK)})",
            p["icon"],
        )

    return (
        f"{_image(with_image)}"
        f"<p><b>{p['icon']} {p['name'].upper()}</b></p>"
        f"<blockquote><i>{p['desc']}</i></blockquote>"
        + _table(rows)
        + f"<p><b>{_info()} {info}</b></p>"
        + buttons
    )


def miners_html(user_id: int, with_image: bool = False) -> str:
    user, m = load(user_id)
    rows = [
        (
            t["name"],
            f"{owned(m, i)}/{t['max']}",
            f"{fmt_rate(miner_income(m, i))}/ч" if owned(m, i) else "-",
        )
        for i, t in enumerate(MINERS)
    ]
    return (
        f"{_image(with_image)}"
        "<p><b>ШАХТЁРЫ</b></p>"
        "<blockquote><i>У каждого уровня шахтёров свой доход, цена и лимит. "
        "Нажми на шахтёра внизу, чтобы открыть его карточку, нанять его "
        "и выдать кирки.</i></blockquote>"
        + _table3(rows, ("Шахтёр", "Нанято", "Доход"))
        + f"<p><b>{_info()} Всего шахтёров: {total_miners(m)}. "
        f"Доход шахты: {fmt_rate(income_per_hour(m))} в час.</b></p>"
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
        ("Шахтёр", t["name"]),
        ("Нанято", f"{have}/{t['max']}"),
        (f"{_income()} Доход без кирки", f"{fmt_rate(t['income'])} в час"),
        (f"{_pick()} Без кирки", str(bare(m, i))),
        (f"{_pick()} С кирками", eq_text),
        (f"{_income()} Доход всех этого уровня", f"{fmt_rate(miner_income(m, i))} в час"),
    ]

    buttons = ""
    if have >= t["max"]:
        info = "Все места для этого уровня заняты. Нанимай шахтёров других уровней."
    else:
        price = miner_price(m, i)
        rows.append((f"{_coin()} Цена найма", fmt(price)))
        rows.append(("Окупится за", f"{fmt_dur(price / t['income'] * 3600)} работы"))
        info = (
            f"Новый {t['name']} принесёт ещё {fmt_rate(t['income'])} монет в час "
            "без кирки. Каждый следующий этого уровня стоит дороже."
        )
        buttons = _button(f"mine:buy_miner:{i}:1", f"Нанять · {fmt(price)}")
        coins = user.get("coins", 0)
        can = max_affordable_hire(m, i, coins)
        if can >= 2:
            buttons += _button(
                f"mine:buy_miner:{i}:0",
                f"Нанять максимум ({can}) · {fmt(hire_cost(m, i, can))}",
            )

    return (
        f"{_image(with_image)}"
        f"<p><b>{t['name'].upper()}</b></p>"
        f"<blockquote><i>{t['desc']}</i></blockquote>"
        + _table(rows)
        + f"<p><b>{_info()} {info}</b></p>"
        + buttons
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
        info = (
            f"Без кирки: {bare(m, i)}. Нажми на кирку, чтобы выдать одну, "
            "или «Все», чтобы выдать сколько получится."
        )

    table = (
        _table(
            [(f"{PICKAXES[p]['icon']} {PICKAXES[p]['name']}", f"{c} шт.") for p, c in free],
            head=("Свободная кирка", "Сколько"),
        )
        if free else ""
    )
    return (
        f"{_image(with_image)}"
        f"<p><b>ВЫДАТЬ КИРКУ: {t['name'].upper()}</b></p>"
        + table
        + f"<p><b>{_info()} {info}</b></p>"
    )


def take_html(user_id: int, i: int, with_image: bool = False) -> str:
    user, m = load(user_id)
    t = MINERS[i]
    eq = eq_of(m, i)

    table = (
        _table(
            [(f"{PICKAXES[p]['icon']} {PICKAXES[p]['name']}", f"{c} шт.")
             for p, c in sorted(eq.items())],
            head=("Надетая кирка", "Сколько"),
        )
        if eq else ""
    )
    info = (
        "Нажми на кирку, чтобы снять одну, или «Все», чтобы снять все такие. "
        "Снятые кирки вернутся в запас."
        if eq else "У шахтёров этого уровня нет кирок."
    )
    return (
        f"{_image(with_image)}"
        f"<p><b>СНЯТЬ КИРКУ: {t['name'].upper()}</b></p>"
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
            [_btn("Кирки", "mine:picks", PICK_EMOJI[0]), _btn("👷 Шахтёры", "mine:miners")],
            [_btn("Авто-выдача кирок", "mine:auto"), _btn("Обновить", "mine:open")],
            _back_row("menu:back"),
        ]
    )


def picks_kb(user_id: int) -> InlineKeyboardMarkup:
    rows = _pairs([_btn(f"{p['icon']} {p['name']}", f"mine:pick:{i}") for i, p in enumerate(PICKAXES)])
    rows.append([_btn("Авто-выдача кирок", "mine:auto")])
    rows.append(_back_row("mine:open"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def miners_kb(user_id: int) -> InlineKeyboardMarkup:
    rows = _pairs([_btn(t["name"], f"mine:miner:{i}") for i, t in enumerate(MINERS)])
    rows.append(_back_row("mine:open"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _back_to(data: str):
    def kb(user_id: int) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[_back_row(data)])
    return kb


def _miner_card_kb(i: int):
    def kb(user_id: int) -> InlineKeyboardMarkup:
        _, m = load(user_id)
        row = [_btn("Выдать кирку", f"mine:give:{i}")]
        if equipped_on(m, i) > 0:  # снимать нечего, пока кирок нет
            row.append(_btn("Снять кирку", f"mine:take:{i}"))
        return InlineKeyboardMarkup(
            inline_keyboard=[row, _back_row("mine:miners")]
        )
    return kb


def _give_kb(i: int):
    def kb(user_id: int) -> InlineKeyboardMarkup:
        _, m = load(user_id)
        rows = []
        if bare(m, i) > 0:
            for p in range(len(PICKAXES)):
                free = picks_free(m, p)
                if free > 0:
                    rows.append([
                        _btn(f"{PICKAXES[p]['icon']} {PICKAXES[p]['name']} ({free})",
                             f"mine:give:{i}:{p}:1"),
                        _btn("Все", f"mine:give:{i}:{p}:0"),
                    ])
        rows.append(_back_row(f"mine:miner:{i}"))
        return InlineKeyboardMarkup(inline_keyboard=rows)
    return kb


def _take_kb(i: int):
    def kb(user_id: int) -> InlineKeyboardMarkup:
        _, m = load(user_id)
        eq = sorted(eq_of(m, i).items())
        rows = [
            [
                _btn(f"{PICKAXES[p]['icon']} {PICKAXES[p]['name']} ({c})",
                     f"mine:take:{i}:{p}:1"),
                _btn("Все", f"mine:take:{i}:{p}:0"),
            ]
            for p, c in eq
        ]
        if len(eq) > 1:
            rows.append([_btn("Снять все кирки", f"mine:takeall:{i}")])
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


def img_key(key: str) -> str:
    """Под каким ключом хранится фото экрана: у всех экранов шахтёров оно одно."""
    if key.startswith(("mine_miner_", "mine_give_", "mine_take_")):
        return "mine_miners"
    return key


async def send_screen(bot, chat_id: int, user_id: int, key: str) -> None:
    html, kb = SCREENS[key]
    await D.send_rich_card(
        bot, chat_id, img_key(key), lambda with_image: html(user_id, with_image), kb(user_id)
    )


async def send_preview(bot, chat_id: int, user_id: int, key: str) -> None:
    """Предпросмотр для /img: без кнопок под сообщением."""
    html, _ = SCREENS[key]
    await D.send_rich_card(
        bot, chat_id, img_key(key), lambda with_image: html(user_id, with_image)
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


def _count(nums: list[int], pos: int, default: int = 1) -> int | None:
    """Количество из callback_data (0 = «максимум»). None, если число кривое."""
    n = nums[pos] if len(nums) > pos else default
    return n if 0 <= n <= MAX_BULK else None


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


def _start_run(m: dict) -> None:
    now = time.time()
    m["run_start"] = now
    m["run_end"] = now + RUN_HOURS * 3600
    m["ts"] = now
    m["runs"] = m.get("runs", 0) + 1


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

    _start_run(m)
    D.save_users()
    await callback.answer(f"Шахта запущена на {RUN_HOURS} ч!")
    await _show(callback, "mine")


@router.callback_query(F.data == "mine:restart")
async def on_restart(callback: CallbackQuery):
    """Забрать добычу со склада и сразу запустить новую смену."""
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

    gain = int(m["stored"])
    m["stored"] -= gain
    user["coins"] += gain
    _start_run(m)
    D.save_users()
    await callback.answer(
        f"Собрано монет: {fmt(gain)}. Шахта запущена на {RUN_HOURS} ч!"
    )
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
    nums = _valid(callback, len(PICKAXES))
    n = _count(nums, 1) if nums else None
    if nums is None or n is None:
        await callback.answer()
        return
    i = nums[0]

    m = get_mine(user)
    p = PICKAXES[i]
    if n == 0:  # «максимум»: сколько хватает монет
        n = min(user["coins"] // p["price"], MAX_BULK)
        if n < 1:
            await callback.answer(
                f"Не хватает монет: {fmt(p['price'] - user['coins'])}", show_alert=True
            )
            return

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


@router.callback_query(F.data.regexp(r"^mine:buy_miner:\d+(:\d+)?$"))
async def on_buy_miner(callback: CallbackQuery):
    user = await _require_user(callback)
    if user is None:
        return
    nums = _valid(callback, len(MINERS))
    n = _count(nums, 1) if nums else None
    if nums is None or n is None:
        await callback.answer()
        return
    i = nums[0]

    m = get_mine(user)
    settle(m)
    t = MINERS[i]

    if owned(m, i) >= t["max"]:
        await callback.answer("Лимит шахтёров этого уровня достигнут.", show_alert=True)
        return

    if n == 0:  # «максимум»
        n = max_affordable_hire(m, i, user["coins"])
        if n < 1:
            await callback.answer(
                f"Не хватает монет: {fmt(miner_price(m, i) - user['coins'])}",
                show_alert=True,
            )
            return
    n = min(n, t["max"] - owned(m, i))

    price = hire_cost(m, i, n)
    if user["coins"] < price:
        await callback.answer(
            f"Не хватает монет: {fmt(price - user['coins'])}", show_alert=True
        )
        return

    user["coins"] -= price
    m["miners"][str(i)] = owned(m, i) + n
    D.save_users()
    await callback.answer(f"Нанят: {t['name']} ×{n}! Теперь их {owned(m, i)}.")
    await _show(callback, f"mine_miner_{i}")


# --- Выдать / снять кирку ---

@router.callback_query(F.data == "mine:auto")
async def on_auto(callback: CallbackQuery):
    """Раздать все кирки оптимально: лучшие лучшим шахтёрам."""
    user = await _require_user(callback)
    if user is None:
        return
    m = get_mine(user)
    settle(m)  # доход меняется: сначала фиксируем добычу по старому

    if not any(picks_owned(m, p) for p in range(len(PICKAXES))):
        await callback.answer("У тебя пока нет кирок. Купи их в разделе «Кирки».", show_alert=True)
        return

    before = income_per_hour(m)
    auto_equip(m)
    after = income_per_hour(m)
    D.save_users()
    if after > before:
        text = f"Кирки розданы. Доход: {fmt_rate(before)} → {fmt_rate(after)} в час"
    else:
        text = "Кирки уже раздаются лучшим образом."
    await callback.answer(text)
    await _show(callback, "mine")


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


@router.callback_query(F.data.regexp(r"^mine:give:\d+:\d+(:\d+)?$"))
async def on_give(callback: CallbackQuery):
    user = await _require_user(callback)
    if user is None:
        return
    nums = _valid(callback, len(MINERS), len(PICKAXES))
    n = _count(nums, 2) if nums else None
    if nums is None or n is None:
        await callback.answer()
        return
    i, p = nums[0], nums[1]

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

    limit = min(bare(m, i), picks_free(m, p))
    k = limit if n == 0 else min(n, limit)

    slot = m["equip"].setdefault(str(i), {})
    slot[str(p)] = int(slot.get(str(p), 0)) + k
    D.save_users()
    await callback.answer(f"{MINERS[i]['name']} получил: {PICKAXES[p]['name']} ×{k}")
    await _show(callback, f"mine_give_{i}")


@router.callback_query(F.data.regexp(r"^mine:take:\d+:\d+(:\d+)?$"))
async def on_take(callback: CallbackQuery):
    user = await _require_user(callback)
    if user is None:
        return
    nums = _valid(callback, len(MINERS), len(PICKAXES))
    n = _count(nums, 2) if nums else None
    if nums is None or n is None:
        await callback.answer()
        return
    i, p = nums[0], nums[1]

    m = get_mine(user)
    settle(m)

    slot = m["equip"].get(str(i), {})
    have = int(slot.get(str(p), 0))
    if have <= 0:
        await callback.answer("Такой кирки у этих шахтёров нет.", show_alert=True)
        return

    k = have if n == 0 else min(n, have)
    slot[str(p)] = have - k
    if slot[str(p)] <= 0:
        del slot[str(p)]
    D.save_users()
    await callback.answer(f"Снято: {PICKAXES[p]['name']} ×{k}. Она вернулась в запас.")
    await _show(callback, f"mine_take_{i}")


@router.callback_query(F.data.regexp(r"^mine:takeall:\d+$"))
async def on_take_all(callback: CallbackQuery):
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

    if equipped_on(m, i) <= 0:
        await callback.answer("У этих шахтёров нет кирок.", show_alert=True)
        return

    m["equip"][str(i)] = {}
    D.save_users()
    await callback.answer("Все кирки сняты и вернулись в запас.")
    await _show(callback, f"mine_take_{i}")
