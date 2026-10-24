"""Раздел «Исследователи».

Игрок нанимает отряды исследователей (10 групп с разной ценой и силой) и отправляет
их в экспедиции на 4 / 8 / 12 / 16 / 24 / 48 часов. Сначала доступна только экспедиция
на 4 часа, остальные длительности покупаются за монеты. Из экспедиции отряд приносит
монеты, мясо, фрукты и, с небольшим шансом, артефакт (редкость до мифической).

Подключение в main.py (см. вызов exrn.setup(...) и dp.include_router(exrn.router)).
Данные игрока лежат в user["exrn"]:
    {"hired": [ключи нанятых групп],
     "times": [открытые длительности в часах],
     "trips": {ключ группы: {"start": ts, "end": ts, "hours": h}}}
"""

from __future__ import annotations

import logging
import math
import random
import time
from html import escape

from aiogram import F, Router
from aiogram.enums import ButtonStyle
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

log = logging.getLogger(__name__)
router = Router()

# Куда админ может добавить фото
IMG_TARGETS: dict[str, str] = {"exrn": "Исследователи"}

# ---------------------------------------------------------------------------
# Настройки баланса: всё, что нужно крутить, собрано здесь
# ---------------------------------------------------------------------------

# Длительности экспедиций (часы) и цена открытия. 4 часа открыты сразу.
DURATIONS: list[int] = [4, 8, 12, 16, 24, 48]
DURATION_PRICE: dict[int, int] = {
    4: 0,
    8: 1_500,
    12: 5_000,
    16: 15_000,
    24: 45_000,
    48: 150_000,
}
# Бонус к добыче за длинную экспедицию (множитель на всё, кроме шанса артефакта)
DURATION_BONUS: dict[int, float] = {4: 1.00, 8: 1.05, 12: 1.10, 16: 1.15, 24: 1.25, 48: 1.50}

SPREAD = (0.75, 1.25)        # разброс добычи: от 75% до 125% от средней
LUCKY_CHANCE = 0.08          # шанс «счастливой находки»: вся добыча x1.5
LUCKY_MULT = 1.5
ART_CHANCE_CAP = 0.30        # шанс артефакта за одну экспедицию никогда не выше 30%
RARITY_DECAY = 0.35          # чем меньше, тем реже выпадают высокие редкости
RARITY_SKILL = 0.35          # насколько сильные отряды чаще находят редкое

# 10 групп исследователей: цена найма и добыча в час.
# art: шанс артефакта за экспедицию на 4 часа (для длинных растёт, см. art_chance()).
GROUPS: list[dict] = [
    {
        "key": "scouts", "icon": "🌱", "name": "Юные следопыты", "price": 500,
        "coins_h": 25, "meat_h": 0.5, "fruit_h": 0.4, "art": 0.020,
        "desc": "Бойкие ребята с картой, нарисованной на салфетке. Знают каждый куст "
                "у околицы и не боятся ничего, кроме маминого голоса.",
    },
    {
        "key": "foresters", "icon": "🌲", "name": "Лесные разведчики", "price": 2_000,
        "coins_h": 50, "meat_h": 0.8, "fruit_h": 0.7, "art": 0.025,
        "desc": "Тихие, как туман между сосен. Читают следы лучше, чем книги, "
                "и всегда возвращаются с полными мешками.",
    },
    {
        "key": "hunters", "icon": "🏹", "name": "Охотники за трофеями", "price": 6_000,
        "coins_h": 95, "meat_h": 1.2, "fruit_h": 1.0, "art": 0.030,
        "desc": "Меткие, жадные до славы и дичи. Над их костром всегда пахнет "
                "жареным мясом и легендами.",
    },
    {
        "key": "guides", "icon": "⛰️", "name": "Горные проводники", "price": 15_000,
        "coins_h": 170, "meat_h": 1.8, "fruit_h": 1.5, "art": 0.035,
        "desc": "Для них скалы лишь лестница. Знают тропы, где не ступала нога "
                "простых смертных, и пещеры, о которых молчат даже эхо.",
    },
    {
        "key": "nomads", "icon": "🏜️", "name": "Пустынные странники", "price": 40_000,
        "coins_h": 300, "meat_h": 2.6, "fruit_h": 2.2, "art": 0.040,
        "desc": "Закалённые солнцем и песком. Находят оазисы по запаху, а погребённые "
                "города по шёпоту ветра.",
    },
    {
        "key": "sailors", "icon": "⚓", "name": "Морские волки", "price": 100_000,
        "coins_h": 520, "meat_h": 3.7, "fruit_h": 3.1, "art": 0.050,
        "desc": "Солёные, шумные и невероятно удачливые. Тянут из глубин сундуки, "
                "которые море прятало веками.",
    },
    {
        "key": "keepers", "icon": "🏛️", "name": "Хранители руин", "price": 250_000,
        "coins_h": 880, "meat_h": 5.2, "fruit_h": 4.4, "art": 0.060,
        "desc": "Учёные-авантюристы. Расшифровывают древние надписи, обходят ловушки "
                "и уносят то, что другие не заметили бы.",
    },
    {
        "key": "shadows", "icon": "🌑", "name": "Теневые искатели", "price": 600_000,
        "coins_h": 1_450, "meat_h": 7.2, "fruit_h": 6.2, "art": 0.070,
        "desc": "Их никто не видит, пока они сами не захотят. Проходят там, где стража "
                "спит, а замки сами открываются.",
    },
    {
        "key": "stars", "icon": "🌠", "name": "Звёздные картографы", "price": 1_500_000,
        "coins_h": 2_350, "meat_h": 10.0, "fruit_h": 8.6, "art": 0.085,
        "desc": "Чертят карты по созвездиям и находят места, которых нет ни на одной "
                "земной карте. Говорят, звёзды подсказывают им, где зарыт клад.",
    },
    {
        "key": "legends", "icon": "👑", "name": "Легенды Края Мира", "price": 4_000_000,
        "coins_h": 3_800, "meat_h": 14.0, "fruit_h": 12.0, "art": 0.100,
        "desc": "Герои, о которых слагают песни. Доходят туда, где кончается мир, "
                "и возвращаются с тем, что мифы считали выдумкой.",
    },
]
BY_KEY = {g["key"]: g for g in GROUPS}

# Фразы при возвращении отряда
RETURN_LINES = [
    "Отряд вернулся, покрытый пылью дорог, но с горящими глазами.",
    "Издалека слышен гомон: исследователи несут добычу и байки.",
    "Усталые, но довольные, они свалили мешки к твоим ногам.",
    "Костры погасли, карты свёрнуты. Дорога осталась позади, добыча впереди.",
    "Они вернулись с рассветом, и от них пахнет приключениями.",
]
LUCKY_LINES = [
    "Сегодня удача улыбнулась им во весь рот: нашли нетронутый тайник!",
    "Невероятное везение! Отряд наткнулся на забытый склад путешественников.",
    "Звёзды сошлись: добыча вдвое тяжелее, чем они рассчитывали!",
]

# ---------------------------------------------------------------------------
# Подключение к боту
# ---------------------------------------------------------------------------

_get_user = None
_save_users = None
_show_rich_card = None
_custom_emoji = None
_drop_artifact = None
_result_block = None
_COIN = ("5449418135381759397", "🪙")
_INFO = ("5334544901428229844", "ℹ️")
_BACK_ID: str | None = None
_MEAT = "🥩"
_FRUIT = "🥭"
_TIERS: list[str] = []


def setup(
    *,
    get_user,
    save_users,
    show_rich_card,
    custom_emoji,
    COIN_EMOJI,
    INFO_EMOJI,
    BACK_EMOJI_ID,
    MEAT_EMOJI,
    FRUIT_EMOJI,
    drop_artifact,
    result_block,
    tiers,
) -> None:
    """drop_artifact(user, tier) выдаёт артефакт игроку, result_block(art) рисует карточку,
    tiers: названия редкостей по порядку от обычной к высшей (list(equipment.TIERS))."""
    global _get_user, _save_users, _show_rich_card, _custom_emoji, _drop_artifact
    global _result_block, _COIN, _INFO, _BACK_ID, _MEAT, _FRUIT, _TIERS
    _get_user = get_user
    _save_users = save_users
    _show_rich_card = show_rich_card
    _custom_emoji = custom_emoji
    _drop_artifact = drop_artifact
    _result_block = result_block
    _COIN, _INFO, _BACK_ID = COIN_EMOJI, INFO_EMOJI, BACK_EMOJI_ID
    _MEAT, _FRUIT = MEAT_EMOJI, FRUIT_EMOJI
    _TIERS = list(tiers)


# ---------------------------------------------------------------------------
# Вспомогательное
# ---------------------------------------------------------------------------

def _num(n: int | float) -> str:
    return f"{int(n):,}".replace(",", " ")


def _coin() -> str:
    return _custom_emoji(_COIN)


def _info() -> str:
    return _custom_emoji(_INFO)


def _dur(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, rest = divmod(seconds, 3600)
    m = rest // 60
    if h and m:
        return f"{h} ч {m} мин"
    if h:
        return f"{h} ч"
    return f"{max(m, 1)} мин"


def _bar(done: float, total: float, width: int = 10) -> str:
    frac = 0 if total <= 0 else min(1.0, max(0.0, done / total))
    full = int(frac * width)
    return "▰" * full + "▱" * (width - full) + f" {int(frac * 100)}%"


def _state(user: dict) -> dict:
    ex = user.get("exrn")
    if not isinstance(ex, dict):
        ex = {}
        user["exrn"] = ex
    ex.setdefault("hired", [])
    ex.setdefault("trips", {})
    times = ex.setdefault("times", [DURATIONS[0]])
    if DURATIONS[0] not in times:
        times.append(DURATIONS[0])
    return ex


def _mythic_index() -> int:
    """Номер мифической редкости (выше неё артефакты из экспедиций не выпадают)."""
    for i, name in enumerate(_TIERS):
        if str(name).lower().startswith("мифич"):
            return i
    return max(0, len(_TIERS) - 1)


def _tier_cap(gi: int) -> int:
    """Самая высокая редкость, которую может найти группа с номером gi (с нуля)."""
    return min(_mythic_index(), 1 + gi // 2)


def _tier_weights(gi: int) -> list[float]:
    cap = _tier_cap(gi)
    return [
        (RARITY_DECAY ** j) * (1 + gi * RARITY_SKILL * j)
        for j in range(cap + 1)
    ]


def art_chance(gi: int, hours: int) -> float:
    base = GROUPS[gi]["art"] * math.sqrt(hours / DURATIONS[0])
    return min(ART_CHANCE_CAP, base)


def _tier_name(i: int) -> str:
    return str(_TIERS[i]) if 0 <= i < len(_TIERS) else "—"


def _kb(*rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[list(r) for r in rows if r])


def _btn(text: str, data: str, style: ButtonStyle = ButtonStyle.PRIMARY) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data, style=style)


def _back(data: str, text: str = "Назад") -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text=text, callback_data=data, style=ButtonStyle.PRIMARY,
        icon_custom_emoji_id=_BACK_ID,
    )


def _pairs(buttons: list[InlineKeyboardButton], size: int = 2) -> list[list[InlineKeyboardButton]]:
    return [buttons[i:i + size] for i in range(0, len(buttons), size)]


def _image(with_image: bool) -> str:
    return '<img src="tg://photo?id=pet"/>' if with_image else ""


def _trip_status(ex: dict, key: str, now: float) -> tuple[str, str]:
    """(код, текст): idle / going / ready / locked."""
    if key not in ex["hired"]:
        return "locked", "не нанят"
    trip = ex["trips"].get(key)
    if not trip:
        return "idle", "свободен"
    if now >= trip["end"]:
        return "ready", "вернулся: забери добычу"
    return "going", f"в пути, ещё {_dur(trip['end'] - now)}"


# ---------------------------------------------------------------------------
# Добыча
# ---------------------------------------------------------------------------

def roll_trip(gi: int, hours: int) -> dict:
    """Считает добычу за экспедицию. Артефакт здесь только выбирается по редкости."""
    g = GROUPS[gi]
    mult = DURATION_BONUS.get(hours, 1.0)
    lucky = random.random() < LUCKY_CHANCE
    if lucky:
        mult *= LUCKY_MULT

    def part(rate: float, minimum: int) -> int:
        value = rate * hours * mult * random.uniform(*SPREAD)
        return max(minimum, int(round(value)))

    result = {
        "coins": part(g["coins_h"], 1),
        "meat": part(g["meat_h"], 1),
        "fruit": part(g["fruit_h"], 1),
        "lucky": lucky,
        "tier": None,
    }
    if random.random() < art_chance(gi, hours):
        weights = _tier_weights(gi)
        result["tier"] = random.choices(range(len(weights)), weights=weights)[0]
    return result


# ---------------------------------------------------------------------------
# Экраны
# ---------------------------------------------------------------------------

def main_html(user: dict, with_image: bool = False) -> str:
    ex = _state(user)
    now = time.time()
    rows = ""
    for g in GROUPS:
        if g["key"] not in ex["hired"]:
            continue
        code, text = _trip_status(ex, g["key"], now)
        mark = {"ready": "✅", "going": "⏳", "idle": "💤"}.get(code, "")
        rows += (
            f"<tr><td><b>{g['icon']} {g['name']}</b></td>"
            f'<td align="center">{mark} {text}</td></tr>'
        )
    hired = len(ex["hired"])
    if rows:
        squads = (
            "<table bordered striped>"
            "<tr><th><b>Отряд</b></th><th><b>Статус</b></th></tr>"
            f"{rows}</table>"
        )
    else:
        squads = (
            "<p><i>Пока ни одного отряда. Загляни в «Нанять отряд»: "
            "первая дорога начинается с первого шага.</i></p>"
        )
    longest = max(ex["times"])
    return (
        f"{_image(with_image)}"
        "<p><b>🧭 ИССЛЕДОВАТЕЛИ</b></p>"
        "<blockquote><i>За краем карты лежат забытые руины, шепчущие леса и пещеры, "
        "полные золота. Нанимай смелых искателей, отправляй их в путь и жди: "
        "дорога щедро платит тем, кто умеет ждать.</i></blockquote>"
        f"{squads}"
        f"<p><b>{_info()} Нанято отрядов: {hired}/{len(GROUPS)} · "
        f"самая длинная экспедиция: {longest} ч</b></p>"
        f"<p><b>Найти можно:</b> {_coin()} монеты, {_MEAT} мясо, {_FRUIT} фрукты "
        "и, если повезёт, редкий артефакт.</p>"
    )


def main_kb(user: dict) -> InlineKeyboardMarkup:
    ex = _state(user)
    now = time.time()
    squad_buttons = []
    for gi, g in enumerate(GROUPS):
        if g["key"] not in ex["hired"]:
            continue
        code, _ = _trip_status(ex, g["key"], now)
        style = {"ready": ButtonStyle.SUCCESS, "going": ButtonStyle.PRIMARY}.get(code, ButtonStyle.PRIMARY)
        mark = {"ready": "✅ ", "going": "⏳ "}.get(code, "")
        squad_buttons.append(_btn(f"{mark}{g['icon']} {g['name']}", f"exrn:g:{gi}", style))
    return _kb(
        *_pairs(squad_buttons),
        [_btn("➕ Нанять отряд", "exrn:list", ButtonStyle.SUCCESS),
         _btn("⏱ Время экспедиций", "exrn:times", ButtonStyle.PRIMARY)],
        [_back("menu:back")],
    )


def list_html(user: dict, with_image: bool = False) -> str:
    ex = _state(user)
    rows = ""
    for gi, g in enumerate(GROUPS):
        owned = g["key"] in ex["hired"]
        price = "✅ нанят" if owned else f"{_num(g['price'])} {_coin()}"
        rows += (
            f"<tr><td><b>{gi + 1}. {g['icon']} {g['name']}</b></td>"
            f'<td align="center">{price}</td></tr>'
        )
    return (
        f"{_image(with_image)}"
        "<p><b>➕ НАЙМ ИССЛЕДОВАТЕЛЕЙ</b></p>"
        "<blockquote><i>Чем дороже отряд, тем богаче добыча и тем выше шанс "
        "вернуться с артефактом. Нанять можно каждого один раз, и он служит тебе навсегда.</i></blockquote>"
        "<table bordered striped>"
        "<tr><th><b>Отряд</b></th><th><b>Цена</b></th></tr>"
        f"{rows}</table>"
        f"<p><b>{_info()} Твои монеты: {_num(user['coins'])} {_coin()}</b></p>"
        "<p><i>Нажми на отряд, чтобы посмотреть подробности.</i></p>"
    )


def list_kb(user: dict) -> InlineKeyboardMarkup:
    ex = _state(user)
    buttons = []
    for gi, g in enumerate(GROUPS):
        owned = g["key"] in ex["hired"]
        buttons.append(_btn(
            f"{'✅' if owned else g['icon']} {gi + 1}",
            f"exrn:g:{gi}",
            ButtonStyle.SUCCESS if owned else ButtonStyle.PRIMARY,
        ))
    return _kb(*_pairs(buttons, 5), [_back("exrn:main")])


def group_html(user: dict, gi: int, with_image: bool = False) -> str:
    g = GROUPS[gi]
    ex = _state(user)
    now = time.time()
    code, status = _trip_status(ex, g["key"], now)
    cap = _tier_cap(gi)
    h0 = DURATIONS[0]
    unlocked = sorted(ex["times"])

    def span(rate: float, hours: int) -> str:
        mult = DURATION_BONUS.get(hours, 1.0)
        return f"{_num(max(1, round(rate * hours * mult * SPREAD[0])))}–" \
               f"{_num(max(1, round(rate * hours * mult * SPREAD[1])))}"

    table = (
        "<table bordered striped>"
        f"<tr><th><b>За {h0} ч</b></th><th><b>Добыча</b></th></tr>"
        f"<tr><td><b>{_coin()} Монеты</b></td><td align=\"center\">{span(g['coins_h'], h0)}</td></tr>"
        f"<tr><td><b>{_MEAT} Мясо</b></td><td align=\"center\">{span(g['meat_h'], h0)}</td></tr>"
        f"<tr><td><b>{_FRUIT} Фрукты</b></td><td align=\"center\">{span(g['fruit_h'], h0)}</td></tr>"
        f"<tr><td><b>💎 Артефакт</b></td><td align=\"center\">"
        f"{art_chance(gi, h0) * 100:.1f}%</td></tr>"
        f"<tr><td><b>✨ Редкость до</b></td><td align=\"center\">{escape(_tier_name(cap))}</td></tr>"
        "</table>"
    )

    head = (
        f"{_image(with_image)}"
        f"<p><b>{g['icon']} {g['name'].upper()}</b></p>"
        f"<blockquote><i>{g['desc']}</i></blockquote>"
        f"{table}"
        "<p><i>Чем дольше экспедиция, тем больше добыча и выше шанс на артефакт.</i></p>"
    )

    if code == "locked":
        return head + (
            f"<p><b>Цена найма: {_num(g['price'])} {_coin()}</b></p>"
            f"<p><b>{_info()} У тебя: {_num(user['coins'])} {_coin()}</b></p>"
        )
    if code == "idle":
        return head + (
            f"<p><b>{_info()} Отряд ждёт приказа. Открытые экспедиции: "
            f"{', '.join(str(t) + ' ч' for t in unlocked)}.</b></p>"
        )
    trip = ex["trips"][g["key"]]
    total = trip["end"] - trip["start"]
    if code == "ready":
        return head + (
            f"<p><b>✅ Отряд вернулся из экспедиции на {trip['hours']} ч!</b></p>"
            "<p><i>Добыча сложена у входа. Осталось её забрать.</i></p>"
        )
    return head + (
        f"<p><b>⏳ В пути: экспедиция на {trip['hours']} ч</b></p>"
        f"<p><b>{_bar(now - trip['start'], total)}</b></p>"
        f"<p><b>Вернутся через {_dur(trip['end'] - now)}</b></p>"
    )


def group_kb(user: dict, gi: int) -> InlineKeyboardMarkup:
    g = GROUPS[gi]
    ex = _state(user)
    code, _ = _trip_status(ex, g["key"], time.time())
    if code == "locked":
        return _kb(
            [_btn(f"Нанять за {_num(g['price'])}", f"exrn:buy:{gi}", ButtonStyle.SUCCESS)],
            [_back("exrn:list")],
        )
    if code == "idle":
        go = [_btn(f"{h} ч", f"exrn:go:{gi}:{h}", ButtonStyle.SUCCESS) for h in sorted(ex["times"])]
        return _kb(
            *_pairs(go, 3),
            [_btn("⏱ Открыть больше времени", "exrn:times", ButtonStyle.PRIMARY)],
            [_back("exrn:main")],
        )
    if code == "ready":
        return _kb(
            [_btn("🎁 Забрать добычу", f"exrn:take:{gi}", ButtonStyle.SUCCESS)],
            [_back("exrn:main")],
        )
    return _kb(
        [_btn("🔄 Обновить", f"exrn:g:{gi}", ButtonStyle.PRIMARY)],
        [_back("exrn:main")],
    )


def times_html(user: dict, with_image: bool = False) -> str:
    ex = _state(user)
    rows = ""
    nxt = _next_time(ex)
    for h in DURATIONS:
        if h in ex["times"]:
            state = "✅ открыто"
        elif h == nxt:
            state = f"{_num(DURATION_PRICE[h])} {_coin()}"
        else:
            state = "🔒 закрыто"
        bonus = int(round((DURATION_BONUS.get(h, 1.0) - 1) * 100))
        rows += (
            f"<tr><td><b>⏱ {h} ч</b></td>"
            f'<td align="center">{"+" + str(bonus) + "%" if bonus else "база"}</td>'
            f'<td align="center">{state}</td></tr>'
        )
    tail = (
        f"<p><b>{_info()} Следующая: {nxt} ч за {_num(DURATION_PRICE[nxt])} {_coin()}</b></p>"
        if nxt else "<p><b>🏆 Все экспедиции открыты!</b></p>"
    )
    return (
        f"{_image(with_image)}"
        "<p><b>⏱ ВРЕМЯ ЭКСПЕДИЦИЙ</b></p>"
        "<blockquote><i>Чем дальше путь, тем щедрее награда. Открывай новые маршруты "
        "по порядку: сначала короткие вылазки, потом дальние походы.</i></blockquote>"
        "<table bordered striped>"
        "<tr><th><b>Время</b></th><th><b>Бонус добычи</b></th><th><b>Статус</b></th></tr>"
        f"{rows}</table>"
        f"{tail}"
        f"<p><b>Твои монеты: {_num(user['coins'])} {_coin()}</b></p>"
    )


def _next_time(ex: dict) -> int | None:
    for h in DURATIONS:
        if h not in ex["times"]:
            return h
    return None


def times_kb(user: dict) -> InlineKeyboardMarkup:
    ex = _state(user)
    nxt = _next_time(ex)
    rows = []
    if nxt:
        rows.append([_btn(f"Открыть {nxt} ч за {_num(DURATION_PRICE[nxt])}", "exrn:tbuy", ButtonStyle.SUCCESS)])
    rows.append([_back("exrn:main")])
    return _kb(*rows)


def loot_html(gi: int, hours: int, loot: dict, art, with_image: bool = False) -> str:
    g = GROUPS[gi]
    line = random.choice(LUCKY_LINES if loot["lucky"] else RETURN_LINES)
    rows = (
        f"<tr><td><b>{_coin()} Монеты</b></td><td align=\"center\"><b>+{_num(loot['coins'])}</b></td></tr>"
        f"<tr><td><b>{_MEAT} Мясо</b></td><td align=\"center\"><b>+{_num(loot['meat'])}</b></td></tr>"
        f"<tr><td><b>{_FRUIT} Фрукты</b></td><td align=\"center\"><b>+{_num(loot['fruit'])}</b></td></tr>"
    )
    text = (
        f"{_image(with_image)}"
        f"<p><b>{g['icon']} {g['name'].upper()}: ДОБЫЧА</b></p>"
        f"<blockquote><i>{line}</i></blockquote>"
        f"<p><b>Экспедиция на {hours} ч{' · ✨ счастливая находка!' if loot['lucky'] else ''}</b></p>"
        "<table bordered striped>"
        "<tr><th><b>Трофей</b></th><th><b>Получено</b></th></tr>"
        f"{rows}</table>"
    )
    if art is not None:
        try:
            text += (
                "<p><b>💎 В ЗАБРОШЕННОМ ТАЙНИКЕ НАШЛИ АРТЕФАКТ!</b></p>"
                f"{_result_block(art)}"
            )
        except Exception:  # карточка не должна ломать выдачу добычи
            log.exception("не удалось нарисовать карточку артефакта")
            text += "<p><b>💎 Отряд принёс артефакт! Загляни в «Снаряжение».</b></p>"
    elif loot["tier"] is not None:
        text += (
            "<p><b>💎 Отряд нашёл артефакт, но такой у тебя уже есть: "
            "его обменяли на монеты.</b></p>"
        )
    return text


# ---------------------------------------------------------------------------
# Обработчики
# ---------------------------------------------------------------------------

async def _need_user(callback: CallbackQuery):
    user = _get_user(callback.from_user.id)
    if user is None:
        await callback.answer("Сначала выбери питомца: /start", show_alert=True)
        return None
    _state(user)
    return user


async def _show(callback: CallbackQuery, html_fn, kb: InlineKeyboardMarkup, force_new: bool = False):
    await _show_rich_card(callback, "exrn", html_fn, kb, force_new=force_new)


@router.callback_query(F.data == "menu:exrn")
async def on_menu(callback: CallbackQuery):
    user = await _need_user(callback)
    if user is None:
        return
    await callback.answer()
    # Из главного меню в раздел: всегда новое сообщение
    await _show(callback, lambda wi: main_html(user, wi), main_kb(user), force_new=True)


@router.callback_query(F.data == "exrn:main")
async def on_main(callback: CallbackQuery):
    user = await _need_user(callback)
    if user is None:
        return
    await callback.answer()
    await _show(callback, lambda wi: main_html(user, wi), main_kb(user))


@router.callback_query(F.data == "exrn:list")
async def on_list(callback: CallbackQuery):
    user = await _need_user(callback)
    if user is None:
        return
    await callback.answer()
    await _show(callback, lambda wi: list_html(user, wi), list_kb(user))


@router.callback_query(F.data.regexp(r"^exrn:g:\d+$"))
async def on_group(callback: CallbackQuery):
    user = await _need_user(callback)
    if user is None:
        return
    gi = int(callback.data.split(":")[2])
    if not 0 <= gi < len(GROUPS):
        await callback.answer("Такого отряда нет", show_alert=True)
        return
    await callback.answer()
    await _show(callback, lambda wi: group_html(user, gi, wi), group_kb(user, gi))


@router.callback_query(F.data.regexp(r"^exrn:buy:\d+$"))
async def on_buy(callback: CallbackQuery):
    user = await _need_user(callback)
    if user is None:
        return
    gi = int(callback.data.split(":")[2])
    if not 0 <= gi < len(GROUPS):
        await callback.answer("Такого отряда нет", show_alert=True)
        return
    g = GROUPS[gi]
    ex = _state(user)
    if g["key"] in ex["hired"]:
        await callback.answer("Этот отряд уже у тебя", show_alert=True)
    elif user["coins"] < g["price"]:
        await callback.answer(
            f"Не хватает монет: нужно {_num(g['price'])}, у тебя {_num(user['coins'])}",
            show_alert=True,
        )
        return
    else:
        user["coins"] -= g["price"]
        ex["hired"].append(g["key"])
        _save_users()
        await callback.answer(f"{g['name']} теперь служат тебе!", show_alert=True)
    await _show(callback, lambda wi: group_html(user, gi, wi), group_kb(user, gi))


@router.callback_query(F.data.regexp(r"^exrn:go:\d+:\d+$"))
async def on_go(callback: CallbackQuery):
    user = await _need_user(callback)
    if user is None:
        return
    _, _, gi_s, h_s = callback.data.split(":")
    gi, hours = int(gi_s), int(h_s)
    if not 0 <= gi < len(GROUPS):
        await callback.answer("Такого отряда нет", show_alert=True)
        return
    g = GROUPS[gi]
    ex = _state(user)
    if g["key"] not in ex["hired"]:
        await callback.answer("Сначала найми этот отряд", show_alert=True)
    elif hours not in ex["times"]:
        await callback.answer("Эта экспедиция ещё не открыта", show_alert=True)
    elif g["key"] in ex["trips"]:
        await callback.answer("Отряд уже в пути", show_alert=True)
    else:
        now = time.time()
        ex["trips"][g["key"]] = {"start": now, "end": now + hours * 3600, "hours": hours}
        _save_users()
        await callback.answer(f"Отряд отправился в путь на {hours} ч!")
    await _show(callback, lambda wi: group_html(user, gi, wi), group_kb(user, gi))


@router.callback_query(F.data.regexp(r"^exrn:take:\d+$"))
async def on_take(callback: CallbackQuery):
    user = await _need_user(callback)
    if user is None:
        return
    gi = int(callback.data.split(":")[2])
    if not 0 <= gi < len(GROUPS):
        await callback.answer("Такого отряда нет", show_alert=True)
        return
    g = GROUPS[gi]
    ex = _state(user)
    trip = ex["trips"].get(g["key"])
    if not trip:
        await callback.answer("Добычи нет: отряд не в экспедиции", show_alert=True)
        await _show(callback, lambda wi: group_html(user, gi, wi), group_kb(user, gi))
        return
    if time.time() < trip["end"]:
        await callback.answer("Отряд ещё в пути", show_alert=True)
        await _show(callback, lambda wi: group_html(user, gi, wi), group_kb(user, gi))
        return

    hours = int(trip["hours"])
    ex["trips"].pop(g["key"], None)          # сначала снимаем экспедицию: награда выдаётся один раз
    loot = roll_trip(gi, hours)
    user["coins"] += loot["coins"]
    user["meat"] += loot["meat"]
    user["fruit"] += loot["fruit"]
    art = None
    if loot["tier"] is not None:
        try:
            art = _drop_artifact(user, _TIERS[loot["tier"]])
        except Exception:
            log.exception("не удалось выдать артефакт из экспедиции")
        if art is None:
            # Дубликат или сбой выдачи: компенсируем монетами, чтобы находка не пропала
            bonus = loot["coins"]
            user["coins"] += bonus
            loot["coins"] += bonus
    _save_users()
    await callback.answer("Добыча у тебя в запасах!")

    kb = _kb(
        [_btn(f"{g['icon']} К отряду", f"exrn:g:{gi}", ButtonStyle.SUCCESS)],
        [_back("exrn:main", "Исследователи")],
    )
    await _show(callback, lambda wi: loot_html(gi, hours, loot, art, wi), kb)


@router.callback_query(F.data == "exrn:times")
async def on_times(callback: CallbackQuery):
    user = await _need_user(callback)
    if user is None:
        return
    await callback.answer()
    await _show(callback, lambda wi: times_html(user, wi), times_kb(user))


@router.callback_query(F.data == "exrn:tbuy")
async def on_time_buy(callback: CallbackQuery):
    user = await _need_user(callback)
    if user is None:
        return
    ex = _state(user)
    nxt = _next_time(ex)
    if nxt is None:
        await callback.answer("Все экспедиции уже открыты", show_alert=True)
    elif user["coins"] < DURATION_PRICE[nxt]:
        await callback.answer(
            f"Не хватает монет: нужно {_num(DURATION_PRICE[nxt])}, у тебя {_num(user['coins'])}",
            show_alert=True,
        )
        return
    else:
        user["coins"] -= DURATION_PRICE[nxt]
        ex["times"].append(nxt)
        ex["times"].sort()
        _save_users()
        await callback.answer(f"Открыта экспедиция на {nxt} ч!", show_alert=True)
    await _show(callback, lambda wi: times_html(user, wi), times_kb(user))
