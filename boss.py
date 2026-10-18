"""Раздел «Босс»: три вида боссов, у каждого свой запас здоровья и награды.

Модуль самостоятельный, как mine.py: main.py не импортирует, нужные функции
передаются один раз через boss.setup(...).

Как это работает:
  * Виды боссов: обычные (100 тыс. HP), сложные (500 тыс. HP), безумные (5 млн HP).
    Одновременно живут до 10 обычных, 5 сложных и 2 безумных (BOSS_TYPES).
  * Сначала игрок выбирает вид, потом конкретного босса из списка, и только потом
    видит его карточку и может атаковать. Состояние всех боссов лежит в boss.json.
  * У каждого питомца 3 врождённых навыка (PET_SKILLS). У навыка свой откат,
    он считается у каждого игрока отдельно.
  * Урон = атака питомца (с учётом уровня и здоровья) × множитель навыка, есть шанс
    крита. Часть навыков пробивает защиту босса.
  * Когда босс падает, награду получают все, кто его бил, пропорционально
    нанесённому урону. Тот, кто добил, получает ещё и бонус.
  * Каждую минуту боссы бьют питомцев своих бойцов. Урон по питомцам у всех
    боссов одинаковый (BOSS_HIT_PCT). Через respawn_min минут убитый босс возрождается.
"""

import asyncio
from collections import deque
import json
import logging
import random
import time
from html import escape
from pathlib import Path
from types import SimpleNamespace

from aiogram import F, Router
from aiogram.enums import ButtonStyle
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    InputRichMessage,
    InputRichMessageMedia,
)

router = Router()

# Заполняется в setup()
D = SimpleNamespace()


def setup(**deps) -> None:
    """Передаём модулю функции и данные из main.py.

    Нужны: get_user, save_users, send_rich_card, custom_emoji, COIN_EMOJI,
    INFO_EMOJI, BACK_EMOJI_ID, PETS, pet_stats, pet_images, STAT_EMOJI.
    """
    for name, value in deps.items():
        setattr(D, name, value)


# ---------- Баланс ----------

BOSS_EMOJI = ("5228962845672096235", "😈")  # кастомный эмодзи босса
FIGHTERS_EMOJI = ("6032609071373226027", "👥")  # бойцов
BLOOD_EMOJI = ("5269535069550162819", "🩸")     # урон
PLACE_EMOJI = ("5413566144986503832", "🏆")     # место
ATTACK_EMOJI = ("5454014806950429357", "⚔️")    # кнопка «Атаковать»

# Виды боссов. Отличаются здоровьем, защитой, наградой, временем возрождения и тем,
# сколько боссов вида может жить одновременно (slots). Урон боссов по питомцам
# у всех видов одинаковый (BOSS_HIT_PCT ниже).
#   hp          здоровье одного босса
#   slots       сколько боссов этого вида одновременно
#   def         обычный урон делится на (1 + защита/100); «пробивающие» навыки её игнорируют
#   respawn_min через сколько минут после смерти босс возрождается
#   reward      награда за убийство, делится между всеми по доле урона
#   last        бонус тому, кто нанёс последний удар
#   names       имена боссов (по одному на каждое место, длина = slots)
BOSS_TYPES: dict[str, dict] = {
    "normal": {
        "title": "Обычные", "one": "Обычный", "icon": "🟢",
        "hp": 100_000, "slots": 10, "def": 10, "respawn_min": 30,
        "reward": {"coins": 20_000, "meat": 100, "fruit": 100},
        "last": {"coins": 2_000, "meat": 10, "fruit": 10},
        "story": (
            "Твари из глубоких пещер. Живут стаями и нападают на каждого, "
            "кто забредёт на их территорию. Хороший выбор для разминки."
        ),
        "names": [
            "Грогх", "Мракон", "Шипоспин", "Гнилозуб", "Каменный Тролль",
            "Болотный Душитель", "Костолом", "Ржавый Голем", "Тёмный Паук", "Ядовитый Слизень",
        ],
    },
    "hard": {
        "title": "Сложные", "one": "Сложный", "icon": "🟠",
        "hp": 500_000, "slots": 5, "def": 20, "respawn_min": 60,
        "reward": {"coins": 120_000, "meat": 600, "fruit": 600},
        "last": {"coins": 12_000, "meat": 60, "fruit": 60},
        "story": (
            "Закалённые чудовища с толстой шкурой и дурным нравом. "
            "Одному питомцу они не по зубам, собирайте целый отряд."
        ),
        "names": ["Рагна Громовая", "Ледяной Страж", "Пепельный Дракон", "Кровавая Королева", "Железный Палач"],
    },
    "insane": {
        "title": "Безумные", "one": "Безумный", "icon": "🔴",
        "hp": 5_000_000, "slots": 2, "def": 35, "respawn_min": 120,
        "reward": {"coins": 1_500_000, "meat": 6_000, "fruit": 6_000},
        "last": {"coins": 150_000, "meat": 600, "fruit": 600},
        "story": (
            "Они выползли из трещины между мирами и с тех пор не знают сна. "
            "Питаются силой чужих питомцев, а их шкура крепче любой брони. "
            "Один неуязвим, но все вместе вы справитесь."
        ),
        "names": ["Ксарвог, Пожиратель Миров", "Нихаррот, Тень Бездны"],
    },
}
TYPE_ORDER = list(BOSS_TYPES)

CRIT_MULT = 2.0          # во сколько раз крит сильнее
CRIT_MAX = 0.6           # шанс крита не выше

TOP_SIZE = 10

# Удары боссов по питомцам (одинаковые у всех видов)
BOSS_HIT_PCT = (0.08, 0.25)  # доля максимального HP питомца за один удар босса (8-25%)
BOSS_STRIKE_INTERVAL = 60    # раз в сколько секунд боссы бьют по своим бойцам
WEAK_MIN = 0.35              # сила и скорость раненого питомца падают плавно до 35% от обычных
                             # (полная форма при 100% HP, минимум при HP = MIN_FIGHT_PCT и ниже)
DODGE_K = 300                # шанс уворота: скорость / (скорость + DODGE_K)
DODGE_MAX = 0.4              # уворот не чаще 40%
REGEN_PCT_PER_MIN = 10       # +10% HP в минуту: полностью питомец восстанавливается за 10 минут
MIN_FIGHT_PCT = 0.15         # HP ниже 15%: питомец уходит на отдых, босс его не бьёт, атаковать он не может
RESUME_PCT = 0.80            # вернуться в бой можно, когда HP снова 80% и выше

# «Лайв»-окно боя
FEED_SIZE = 10        # сколько последних событий хранить (у каждого босса своя лента)
FEED_SHOW = 3         # сколько последних событий показывать в ленте боя
LIVE_INTERVAL = 60    # окна остальных бойцов обновляются не чаще раза в минуту
KB_REFRESH = 3        # кнопки навыков (откаты) обновляются каждые 3 секунды
NOTIFY_SPAWN_TYPES = ("hard", "insane")  # о возрождении каких боссов сообщать тем, кто их бил
SPAWN_CHECK = 5       # как часто проверять, не возродился ли босс (секунды)
VIEW_TTL = 30 * 60    # окно перестаёт обновляться, если игрок давно ничего не нажимал

# Врождённые навыки питомцев (ключи питомцев как в PETS в main.py).
# mult  — множитель атаки за один удар
# hits  — сколько ударов за одно применение
# cd    — откат в секундах
# pierce — игнорирует защиту босса
# crit  — прибавка к шансу крита
# pct   — дополнительно этот % текущего здоровья босса (но не больше атака × 4)
PET_SKILLS: dict[str, list[dict]] = {
    "flame": [
        {"name": "Огненный шар", "icon": "🔥", "mult": 1.5, "hits": 1, "cd": 30,
         "desc": "Быстрый сгусток пламени. Короткий откат."},
        {"name": "Пылающий дождь", "icon": "☄️", "mult": 0.6, "hits": 4, "cd": 90,
         "desc": "Четыре удара подряд, у каждого свой шанс крита."},
        {"name": "Извержение", "icon": "🌋", "mult": 4.0, "hits": 1, "cd": 240,
         "crit": 0.25, "desc": "Мощный взрыв с повышенным шансом крита."},
    ],
    "aqua": [
        {"name": "Ледяной шип", "icon": "🧊", "mult": 1.4, "hits": 1, "cd": 30,
         "pierce": True, "desc": "Пробивает защиту босса."},
        {"name": "Водоворот", "icon": "🌀", "mult": 0.9, "hits": 3, "cd": 90,
         "desc": "Три удара потоком воды."},
        {"name": "Цунами", "icon": "🌊", "mult": 3.5, "hits": 1, "cd": 240,
         "pct": 0.02, "desc": "Мощный удар и ещё 2% текущего здоровья босса."},
    ],
    "terra": [
        {"name": "Каменный кулак", "icon": "🪨", "mult": 1.6, "hits": 1, "cd": 30,
         "desc": "Тяжёлый удар. Короткий откат."},
        {"name": "Дрожь земли", "icon": "🌍", "mult": 1.0, "hits": 3, "cd": 90,
         "desc": "Три подземных толчка подряд."},
        {"name": "Обвал", "icon": "⛰️", "mult": 4.5, "hits": 1, "cd": 240,
         "pierce": True, "desc": "Самый сильный удар, пробивает защиту босса."},
    ],
}

# Куда админ может добавить фото через /img
IMG_TARGETS: dict[str, str] = {
    "boss": "Босс: выбор вида",
    "boss_list": "Босс: список боссов",
    "boss_info": "Босс: карточка",
    "boss_skills": "Босс: навыки",
    "boss_top": "Босс: топ бойцов",
}


# ---------- Состояние боссов (общее для всех) ----------

BOSS_FILE = Path(__file__).with_name("boss.json")


def all_ids(btype: str | None = None) -> list[str]:
    """Идентификаторы боссов вида «normal:0», «normal:1», ..."""
    types = [btype] if btype else TYPE_ORDER
    return [f"{t}:{n}" for t in types for n in range(BOSS_TYPES[t]["slots"])]


def type_of(bid: str) -> str:
    return bid.split(":")[0]


def cfg(bid: str) -> dict:
    return BOSS_TYPES[type_of(bid)]


def name_of(bid: str) -> str:
    return cfg(bid)["names"][int(bid.split(":")[1])]


def _fresh(btype: str, kills: int = 0) -> dict:
    return {
        "hp": BOSS_TYPES[btype]["hp"],
        "dmg": {},           # {"user_id": нанесённый урон}
        "names": {},         # {"user_id": ник для топа}
        "dead_until": None,  # когда возродится, если босс убит
        "kills": kills,      # сколько раз босса уже убивали
        "killer": None,      # ник того, кто добил в последний раз
        "last_uid": None,
    }


def _load() -> dict[str, dict]:
    try:
        data = json.loads(BOSS_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    saved = data.get("bosses")
    if saved is None and "hp" in data:  # старый формат: один босс, он становится первым обычным
        saved = {"normal:0": data}
    saved = saved or {}

    result: dict[str, dict] = {}
    for bid in all_ids():
        base = _fresh(type_of(bid))
        base.update({k: v for k, v in saved.get(bid, {}).items() if k in base})
        base["hp"] = min(base["hp"], cfg(bid)["hp"])
        result[bid] = base
    return result


BOSSES: dict[str, dict] = _load()


def _load_totals() -> dict[str, dict]:
    """Урон каждого игрока по всем боссам за всё время: {uid: {"dmg": число, "name": ник}}."""
    try:
        data = json.loads(BOSS_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    totals = data.get("totals")
    if totals is None:  # первый запуск: берём урон из текущих боёв
        totals = {}
        for b in BOSSES.values():
            for uid, dmg in b["dmg"].items():
                t = totals.setdefault(uid, {"dmg": 0, "name": "Игрок"})
                t["dmg"] += int(dmg)
                t["name"] = b["names"].get(uid, t["name"])
    return totals


TOTALS: dict[str, dict] = _load_totals()


def save_boss() -> None:
    BOSS_FILE.write_text(
        json.dumps({"bosses": BOSSES, "totals": TOTALS}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def totals_ranking() -> list[tuple[str, dict]]:
    return sorted(TOTALS.items(), key=lambda kv: kv[1]["dmg"], reverse=True)


def totals_place(uid: str) -> int | None:
    for n, (u, _) in enumerate(totals_ranking(), start=1):
        if u == uid:
            return n
    return None


def is_dead(bid: str) -> bool:
    return BOSSES[bid]["dead_until"] is not None


# Очередь уведомлений о возрождении: [(id босса, [id игроков, что его бил])]
_spawned: list[tuple[str, list[str]]] = []


def tick() -> None:
    """Возродить всех боссов, у которых вышло время."""
    now = time.time()
    changed = False
    for bid, b in list(BOSSES.items()):
        if b["dead_until"] is not None and now >= b["dead_until"]:
            if type_of(bid) in NOTIFY_SPAWN_TYPES and b["dmg"]:
                _spawned.append((bid, list(b["dmg"])))
            BOSSES[bid] = _fresh(type_of(bid), b["kills"])
            changed = True
    if changed:
        save_boss()


def alive_count(btype: str) -> int:
    return sum(1 for bid in all_ids(btype) if not is_dead(bid))


def ranking(bid: str) -> list[tuple[str, int]]:
    return sorted(BOSSES[bid]["dmg"].items(), key=lambda kv: kv[1], reverse=True)


def place_of(bid: str, uid: str) -> int | None:
    for n, (u, _) in enumerate(ranking(bid), start=1):
        if u == uid:
            return n
    return None


def resolve(ref: str) -> str:
    """Из ссылки (id босса, вид или пусто) делает id существующего босса."""
    if ref in BOSSES:
        return ref
    btype = ref if ref in BOSS_TYPES else TYPE_ORDER[0]
    ids = all_ids(btype)
    return next((b for b in ids if not is_dead(b)), ids[0])


# ---------- Расчёты ----------

def fmt(n: float) -> str:
    return f"{int(n):,}".replace(",", " ")


def fmt_dur(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, rest = divmod(seconds, 3600)
    mins, secs = divmod(rest, 60)
    if hours:
        return f"{hours} ч {mins} мин"
    if mins:
        return f"{mins} мин {secs} сек" if secs else f"{mins} мин"
    return f"{secs} сек"


def hp_ratio(bid: str) -> float:
    return max(0.0, BOSSES[bid]["hp"] / cfg(bid)["hp"])


def bar(ratio: float, width: int = 10) -> str:
    """Шкала в скобках: [▰▰▰▰▰▰▱▱▱▱]."""
    ratio = max(0.0, min(1.0, ratio))
    filled = 0 if ratio <= 0 else max(1, round(ratio * width))
    return "[" + "▰" * filled + "▱" * (width - filled) + "]"


def hp_bar(bid: str, width: int = 10) -> str:
    return bar(hp_ratio(bid), width)


def skills_of(user: dict) -> list[dict]:
    return PET_SKILLS[user["pet"]]


def cd_left(user: dict, i: int) -> float:
    last = user.get("boss_cd", {}).get(str(i), 0)
    return max(0.0, last + skills_of(user)[i]["cd"] - time.time())


# ---------- Питомец в бою: здоровье, скорость, удары босса ----------

def _stats_of(user: dict) -> dict:
    return D.pet_stats(D.PETS[user["pet"]], user["level"])


def pet_max_hp(user: dict) -> int:
    return max(1, _stats_of(user)["hp"])


def pet_hp(user: dict) -> float:
    """Текущее здоровье питомца с учётом восстановления со временем."""
    mx = pet_max_hp(user)
    cur = user.get("boss_hp")
    if cur is None:
        return float(mx)
    passed = max(0.0, time.time() - user.get("boss_hp_ts", time.time()))
    return min(float(mx), cur + mx * REGEN_PCT_PER_MIN / 100 * passed / 60)


def _set_pet_hp(user: dict, hp: float) -> None:
    user["boss_hp"] = max(0.0, hp)
    user["boss_hp_ts"] = time.time()


def is_resting(user: dict) -> bool:
    """Питомец отдыхает после боя: босс его не бьёт, а он не может атаковать.
    Отдых включается, когда HP падает ниже MIN_FIGHT_PCT, и длится, пока HP не поднимется до RESUME_PCT."""
    mx = pet_max_hp(user)
    hp = pet_hp(user)
    resting = bool(user.get("boss_rest"))
    if not resting and hp < mx * MIN_FIGHT_PCT:
        resting = True
    elif resting and hp >= mx * RESUME_PCT:
        resting = False
    user["boss_rest"] = resting
    return resting


def pet_ready_in(user: dict) -> float:
    """Через сколько секунд питомец вернётся в бой, то есть дойдёт до RESUME_PCT (0, если не отдыхает)."""
    if not is_resting(user):
        return 0.0
    mx = pet_max_hp(user)
    return max(0.0, mx * RESUME_PCT - pet_hp(user)) / (mx * REGEN_PCT_PER_MIN / 100 / 60)


def pet_form(user: dict) -> float:
    """Боевая форма питомца: 1.0 при полном HP, плавно падает до WEAK_MIN.
    Чем меньше здоровья, тем слабее урон и скорость."""
    share = pet_hp(user) / pet_max_hp(user)
    t = (share - MIN_FIGHT_PCT) / (1 - MIN_FIGHT_PCT)
    return WEAK_MIN + (1 - WEAK_MIN) * max(0.0, min(1.0, t))


def pet_speed(user: dict) -> int:
    """Скорость с учётом ранений."""
    return max(1, round(_stats_of(user)["spd"] * pet_form(user)))


def dodge_chance(user: dict) -> float:
    spd = pet_speed(user)
    return min(DODGE_MAX, spd / (spd + DODGE_K))


def boss_strike(user: dict) -> dict:
    """Удар босса по питомцу: 8-25% от максимального HP. Скорость даёт шанс увернуться."""
    hp = pet_hp(user)
    if random.random() < dodge_chance(user):
        _set_pet_hp(user, hp)
        return {"dodge": True, "dmg": 0, "hp": hp}

    dmg = max(1, round(pet_max_hp(user) * random.uniform(*BOSS_HIT_PCT)))
    hp = max(0.0, hp - dmg)
    _set_pet_hp(user, hp)
    is_resting(user)  # если HP упало ниже порога, питомец уходит на отдых
    return {"dodge": False, "dmg": dmg, "hp": hp}


def roll_attack(user: dict, skill: dict, bid: str) -> tuple[int, list[tuple[int, bool]]]:
    """Считает урон навыка. Возвращает (общий урон, [(урон удара, крит)])."""
    stats = D.pet_stats(D.PETS[user["pet"]], user["level"])
    atk, luck = stats["atk"] * pet_form(user), stats["luck"]
    crit_chance = min(CRIT_MAX, luck / 400 + skill.get("crit", 0.0))
    defense = cfg(bid)["def"]

    hits: list[tuple[int, bool]] = []
    for _ in range(skill["hits"]):
        dmg = atk * skill["mult"] * random.uniform(0.9, 1.1)
        if not skill.get("pierce"):
            dmg *= 100 / (100 + defense)
        crit = random.random() < crit_chance
        if crit:
            dmg *= CRIT_MULT
        hits.append((max(1, round(dmg)), crit))

    total = sum(d for d, _ in hits)
    if skill.get("pct"):
        total += min(round(BOSSES[bid]["hp"] * skill["pct"]), round(atk * 4))
    return total, hits


def finish_boss(bid: str, killer_name: str) -> dict[str, dict[str, int]]:
    """Босс убит: выдаём награды, ставим время возрождения.
    Возвращает {user_id: {"coins":..., "meat":..., "fruit":..., "last": 0/1}}."""
    b, c = BOSSES[bid], cfg(bid)
    total = sum(b["dmg"].values()) or 1
    rewards: dict[str, dict[str, int]] = {}

    for uid, dmg in b["dmg"].items():
        user = D.get_user(int(uid))
        if user is None:
            continue
        share = dmg / total
        r = {
            "coins": max(1, round(c["reward"]["coins"] * share)),
            "meat": max(1, round(c["reward"]["meat"] * share)),
            "fruit": max(1, round(c["reward"]["fruit"] * share)),
            "last": 0,
        }
        if uid == b.get("last_uid"):
            r["coins"] += c["last"]["coins"]
            r["meat"] += c["last"]["meat"]
            r["fruit"] += c["last"]["fruit"]
            r["last"] = 1
        user["coins"] += r["coins"]
        user["meat"] += r["meat"]
        user["fruit"] += r["fruit"]
        rewards[uid] = r

    b["dead_until"] = time.time() + c["respawn_min"] * 60
    b["kills"] += 1
    b["killer"] = killer_name
    D.save_users()
    save_boss()
    return rewards


# ---------- Оформление ----------

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


def _table_n(head: tuple, rows: list[tuple]) -> str:
    """Таблица с любым числом колонок: первая слева, остальные по центру."""
    body = "".join(
        "<tr>"
        + f"<td><b>{r[0]}</b></td>"
        + "".join(f'<td align="center"><b>{c}</b></td>' for c in r[1:])
        + "</tr>"
        for r in rows
    )
    return (
        "<table bordered striped>"
        "<tr>" + "".join(f"<th><b>{h}</b></th>" for h in head) + "</tr>"
        f"{body}"
        "</table>"
    )


def _tree(rows: list, title: str = "") -> str:
    """Блок с рамочкой ├ └. Строка: (подпись, значение) или
    (подпись, значение, [вложенные (подпись, значение)])."""
    lines = []
    for n, row in enumerate(rows):
        label, value = row[0], row[1]
        kids = row[2] if len(row) > 2 else []
        last = n == len(rows) - 1
        line = f"{'└' if last else '├'} {label}"
        if value != "":
            line += f": <b>{value}</b>"
        lines.append(line)
        pad = "&nbsp;&nbsp;&nbsp;" if last else "│&nbsp;&nbsp;"
        for k, (kl, kv) in enumerate(kids):
            kline = f"{pad}{'└' if k == len(kids) - 1 else '├'} {kl}"
            if kv != "":
                kline += f": <b>{kv}</b>"
            lines.append(kline)
    head = f"<b>{title}</b><br>" if title else ""
    return f"<p>{head}{'<br>'.join(lines)}</p>"


def _gap() -> str:
    """Пустая строка-отступ между блоками."""
    return "<p>&nbsp;</p>"


def _stat_icon(key: str) -> str:
    return D.custom_emoji(D.STAT_EMOJI[key])


def _button(data: str, label: str, emoji: str = "", style: str = "success") -> str:
    text = f"{emoji} {label}".strip()
    return (
        "<tg-button-row>"
        f'<tg-button type="callback_data" data="{data}" '
        f'style="{style}">{text}</tg-button>'
        "</tg-button-row>"
    )


def _boss_icon() -> str:
    return D.custom_emoji(BOSS_EMOJI)


def _hp_icon() -> str:
    """Тот же эмодзи здоровья, что и в характеристиках питомца."""
    return D.custom_emoji(D.STAT_EMOJI["hp"])


def _info() -> str:
    return D.custom_emoji(D.INFO_EMOJI)


def _coin() -> str:
    return D.custom_emoji(D.COIN_EMOJI)


def _image(with_image: bool) -> str:
    return '<img src="tg://photo?id=pet"/>' if with_image else ""


def skill_note(sk: dict) -> str:
    """Особенность навыка словами (пусто, если её нет)."""
    notes = []
    if sk.get("pierce"):
        notes.append("пробивает защиту босса")
    if sk.get("crit"):
        notes.append(f"+{round(sk['crit'] * 100)}% к шансу крита")
    if sk.get("pct"):
        notes.append(f"+{round(sk['pct'] * 100)}% текущего здоровья босса")
    return ", ".join(notes)


def pet_skills_html(pet_key: str, with_image: bool = False) -> str:
    """Отдельный экран навыков питомца (открывается кнопкой «Навыки» на карточке)."""
    pet = D.PETS[pet_key]
    skills = PET_SKILLS[pet_key]

    rows = "".join(
        "<tr>"
        f"<td><b>{sk['icon']} {escape(sk['name'])}</b></td>"
        f'<td align="center"><b>{sk["hits"]} × {sk["mult"]:g}</b></td>'
        f'<td align="center"><b>{fmt_dur(sk["cd"])}</b></td>'
        "</tr>"
        for sk in skills
    )
    lines = []
    for sk in skills:
        note = skill_note(sk)
        lines.append(
            f"<b>{sk['icon']} {escape(sk['name'])}</b><br>"
            f"<i>{escape(sk['desc'])}{(' Ещё: ' + note + '.') if note else ''}</i>"
        )
    return (
        f"{_image(with_image)}"
        f"<p><b>{escape(pet['name'].upper())} · НАВЫКИ</b>"
        "<br>&nbsp;<br>"
        "<b>Врождённые навыки для боя с боссом</b></p>"
        "<table bordered striped>"
        "<tr><th><b>Навык</b></th><th><b>Удары</b></th><th><b>Откат</b></th></tr>"
        f"{rows}"
        "</table>"
        "<p><i>Удары: сколько раз бьёт × множитель атаки питомца.</i></p>"
        "<p><b>Описание навыков</b></p>"
        "<blockquote>" + "<br>&nbsp;<br>".join(lines) + "</blockquote>"
    )


# ---------- Экраны ----------

# Ленты последних ударов, у каждого босса своя (для «лайв»-окна)
FEEDS: dict[str, deque] = {}


def _feed(bid: str) -> deque:
    return FEEDS.setdefault(bid, deque(maxlen=FEED_SIZE))


def nick_of(u) -> str:
    """Ник в телеграме (@name), а если его нет, то имя."""
    return f"@{u.username}" if u.username else (u.first_name or "Игрок")


def feed_push(bid: str, kind: str, uid: str, name: str, **extra) -> None:
    """kind: hit (удар игрока), wave (босс ударил по бойцам), kill (босс повержен)."""
    _feed(bid).appendleft({"kind": kind, "uid": uid, "name": name, **extra})


def _head(bid: str, sub: str = "") -> str:
    """Шапка как у карточек питомцев: имя · вид, пустая строка, подзаголовок."""
    c = cfg(bid)
    return (
        f"<p><b>{_boss_icon()} {escape(name_of(bid).upper())} · {c['one'].upper()} БОСС</b>"
        + (f"<br>&nbsp;<br>{sub}" if sub else "")
        + "</p>"
    )


def _reward_tree(c: dict) -> str:
    reward = [
        (f"{_coin()} Монеты", fmt(c["reward"]["coins"])),
        ("🥩 Мясо", fmt(c["reward"]["meat"])),
        ("🥭 Фрукты", fmt(c["reward"]["fruit"])),
        ("🎯 Последний удар", "", [
            (f"{_coin()} Монеты", f"+{fmt(c['last']['coins'])}"),
            ("🥩 Мясо", f"+{c['last']['meat']}"),
            ("🥭 Фрукты", f"+{c['last']['fruit']}"),
        ]),
    ]
    return _tree(reward, "Награда за победу (делится по доле урона)")


def boss_html(user_id: int, with_image: bool = False, ref: str = "") -> str:
    """Первое окно: выбор вида босса."""
    tick()
    rows = [
        (f"{BOSS_TYPES[t]['icon']} {BOSS_TYPES[t]['title']}",
         f"{alive_count(t)}/{BOSS_TYPES[t]['slots']}",
         fmt(BOSS_TYPES[t]["hp"]))
        for t in TYPE_ORDER
    ]
    rewards = [
        (f"{BOSS_TYPES[t]['icon']} {BOSS_TYPES[t]['title']}",
         f"{fmt(BOSS_TYPES[t]['reward']['coins'])} {_coin()}")
        for t in TYPE_ORDER
    ]
    return (
        f"{_image(with_image)}"
        f"<p><b>{_boss_icon()} МИРОВЫЕ БОССЫ</b><br>&nbsp;<br><b>Выбери, с кем сразиться</b></p>"
        "<blockquote><i>Чем сложнее босс, тем богаче награда. "
        "Урон боссов по питомцам у всех одинаковый.</i></blockquote>"
        + _table_n(("Вид", "Живых", "Здоровье"), rows)
        + _gap()
        + _tree(rewards, "Награда за победу (делится по доле урона)")
    )


def boss_list_html(user_id: int, with_image: bool = False, ref: str = "") -> str:
    """Список боссов выбранного вида."""
    tick()
    t = ref if ref in BOSS_TYPES else TYPE_ORDER[0]
    c = BOSS_TYPES[t]
    about = [
        (f"{_hp_icon()} Здоровье", fmt(c["hp"])),
        (f"{_stat_icon('def')} Защита", str(c["def"])),
        ("⏳ Возрождение", f"через {fmt_dur(c['respawn_min'] * 60)}"),
        (f"{_coin()} Награда", fmt(c["reward"]["coins"])),
    ]
    fighters = D.custom_emoji(FIGHTERS_EMOJI)
    rows = []
    for bid in all_ids(t):
        b = BOSSES[bid]
        if is_dead(bid):
            rows.append((f"☠️ {escape(name_of(bid))}",
                         f"вернётся через {fmt_dur(b['dead_until'] - time.time())}"))
        else:
            rows.append((escape(name_of(bid)),
                         f"{hp_ratio(bid) * 100:.0f}% {fighters} {len(b['dmg'])}"))
    return (
        f"{_image(with_image)}"
        f"<p><b>{c['icon']} {c['title'].upper()} БОССЫ</b><br>&nbsp;<br><b>Выбери босса</b></p>"
        f"<blockquote><i>{escape(c['story'])}</i></blockquote>"
        + _tree(about, "Характеристики")
        + _gap()
        + _tree(rows, f"Боссы (живых {alive_count(t)} из {c['slots']})")
    )


def boss_info_html(user_id: int, with_image: bool = False, ref: str = "") -> str:
    """Карточка конкретного босса: кто это и что можно получить."""
    tick()
    bid = resolve(ref)
    c, b = cfg(bid), BOSSES[bid]
    if is_dead(bid):
        status = ("☠️ Статус", f"повержен, вернётся через {fmt_dur(b['dead_until'] - time.time())}")
    else:
        status = (f"{_hp_icon()} Здоровье", f"{fmt(b['hp'])} / {fmt(c['hp'])}")
    about = [
        status,
        (f"{_stat_icon('def')} Защита", str(c["def"])),
        ("⏳ Возрождение", f"через {fmt_dur(c['respawn_min'] * 60)}"),
    ]
    return (
        f"{_image(with_image)}"
        + _head(bid, f"<b>{c['icon']} {c['one']} босс</b>")
        + f"<blockquote><i>{escape(c['story'])}</i></blockquote>"
        + _tree(about, "О боссе")
        + _gap()
        + _reward_tree(c)
        + _gap()
        + _button(f"boss:skills:{bid}", "Атаковать", D.custom_emoji(ATTACK_EMOJI), "danger")
    )


def skills_html(user_id: int, with_image: bool = False, ref: str = "") -> str:
    """Окно боя: здоровье босса, твоя статистика и лента ударов других игроков."""
    tick()
    bid = resolve(ref)
    c, b = cfg(bid), BOSSES[bid]
    uid = str(user_id)
    place = place_of(bid, uid)

    if is_dead(bid):
        sub = (
            "<b>☠️ Повержен</b><br>"
            f"<i>Вернётся через {fmt_dur(b['dead_until'] - time.time())}</i>"
        )
    else:
        sub = (
            f"<b>{_hp_icon()} {fmt(b['hp'])} / {fmt(c['hp'])}</b><br>"
            f"<b>{hp_bar(bid)} {hp_ratio(bid) * 100:.1f}%</b>"
        )

    stats = [
        (f"{D.custom_emoji(FIGHTERS_EMOJI)} Бойцов", str(len(b["dmg"]))),
        (f"{D.custom_emoji(BLOOD_EMOJI)} Твой урон", fmt(b["dmg"].get(uid, 0))),
        (f"{D.custom_emoji(PLACE_EMOJI)} Твоё место", str(place) if place else "—"),
    ]
    pet_rows: list = []

    pet_part = ""
    user = D.get_user(user_id)
    if user is not None:
        pet_name = D.PETS[user["pet"]]["name"]
        mx, hp = pet_max_hp(user), pet_hp(user)
        spd = pet_speed(user)
        base_spd = _stats_of(user)["spd"]
        form = pet_form(user)
        wait = pet_ready_in(user)
        pet_part = (
            f"<p><b>{escape(pet_name.upper())} · ТВОЙ ПИТОМЕЦ</b><br>&nbsp;<br>"
            f"<b>{_hp_icon()} {int(hp)} / {mx}</b><br>"
            f"<b>{bar(hp / mx)} {hp / mx * 100:.0f}%</b></p>"
        )
        pet_rows = [
            (f"{D.custom_emoji(ATTACK_EMOJI)} Сила", f"{form * 100:.0f}%"),
            (f"{_stat_icon('spd')} Скорость", f"{spd} / {base_spd}"),
            ("💨 Уворот", f"{dodge_chance(user) * 100:.0f}%"),
        ]
        if wait > 0:
            pet_rows.append(("😴 Отдыхает", f"вернётся в бой через {fmt_dur(wait)}"))

    feed_items = list(_feed(bid))[:FEED_SHOW]
    if feed_items:
        blood = D.custom_emoji(BLOOD_EMOJI)
        lines = []
        for e in feed_items:
            who = escape(e["name"])
            if e["kind"] == "kill":
                lines.append(f"<i>☠️ {who} добил босса</i>")
            elif e["kind"] == "hit":
                crit = " ⚡" if e["crit"] else ""
                lines.append(
                    f"<i>{e['icon']} {who} · {escape(e['skill'])} · "
                    f"{fmt(e['dmg'])} {blood}{crit}</i>"
                )
            else:  # wave: босс ударил по бойцам
                text = f"{_boss_icon()} Босс ударил по бойцам: {e['hit']}"
                if e["dodged"]:
                    text += f" · увернулись: {e['dodged']}"
                lines.append(f"<i>{text}</i>")
        feed = "<blockquote>" + "<br>".join(lines) + "</blockquote>"
    else:
        feed = "<blockquote><i>Пока тихо. Нанеси первый удар.</i></blockquote>"

    return (
        f"{_image(with_image)}"
        + _head(bid, sub)
        + pet_part
        + (_tree(pet_rows) if pet_rows else "")
        + _gap()
        + _table(stats, head=("Бой", "Значение"))
        + _gap()
        + "<p><b>Лента боя</b></p>"
        + feed
    )


def top_html(user_id: int, with_image: bool = False, ref: str = "") -> str:
    """Общий топ бойцов: сколько урона игрок нанёс всем боссам за всё время."""
    uid = str(user_id)
    rank = totals_ranking()
    total = sum(t["dmg"] for _, t in rank) or 1

    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    rows = [
        (
            f"{medals.get(n, str(n) + '.')} {escape(str(t['name']))}",
            f"{fmt(t['dmg'])} · {t['dmg'] / total * 100:.1f}%",
        )
        for n, (_, t) in enumerate(rank[:TOP_SIZE], start=1)
    ]
    me = totals_place(uid)
    if rows:
        body = _table(rows, head=("Игрок", "Урон · доля"))
    else:
        body = "<p><i>Пока никто не бил боссов. Стань первым.</i></p>"
    tail = (
        _tree([
            (f"{D.custom_emoji(PLACE_EMOJI)} Твоё место", str(me)),
            (f"{D.custom_emoji(BLOOD_EMOJI)} Твой урон", fmt(TOTALS[uid]["dmg"])),
        ], "Твой результат")
        if me else "<p><i>Ты ещё не бил боссов.</i></p>"
    )
    return (
        f"{_image(with_image)}"
        + f"<p><b>{D.custom_emoji(PLACE_EMOJI)} ТОП БОЙЦОВ</b><br>&nbsp;<br>"
        + "<b>Весь урон по всем боссам за всё время</b></p>"
        + body
        + _gap()
        + tail
    )


# ---------- Клавиатуры ----------

def _btn(text: str, data: str, emoji_id: str | None = None,
         style: ButtonStyle = ButtonStyle.PRIMARY) -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text=text, callback_data=data, style=style, icon_custom_emoji_id=emoji_id
    )


def _back_row(data: str) -> list[InlineKeyboardButton]:
    return [_btn("Назад", data, D.BACK_EMOJI_ID)]


def boss_kb(user_id: int, ref: str = "") -> InlineKeyboardMarkup:
    """Выбор вида босса: три кнопки в ряд только с эмодзи, ниже общий топ бойцов."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_btn(BOSS_TYPES[t]["icon"], f"boss:list:{t}") for t in TYPE_ORDER],
            [_btn("Топ бойцов", "boss:top", PLACE_EMOJI[0])],
            _back_row("menu:back"),
        ]
    )


def list_kb(user_id: int, ref: str = "") -> InlineKeyboardMarkup:
    """Боссы выбранного вида: кнопки по две в ряд."""
    t = ref if ref in BOSS_TYPES else TYPE_ORDER[0]
    buttons = []
    for bid in all_ids(t):
        if is_dead(bid):
            buttons.append(_btn(f"☠️ {name_of(bid).split(',')[0]}", f"boss:info:{bid}",
                                style=ButtonStyle.DANGER))
        else:
            buttons.append(_btn(f"{name_of(bid).split(',')[0]} · {hp_ratio(bid) * 100:.0f}%",
                                f"boss:info:{bid}", style=ButtonStyle.SUCCESS))
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    rows.append(_back_row("boss:open"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def info_kb(user_id: int, ref: str = "") -> InlineKeyboardMarkup:
    bid = resolve(ref)
    return InlineKeyboardMarkup(inline_keyboard=[_back_row(f"boss:list:{type_of(bid)}")])


def _short_dur(seconds: float) -> str:
    secs = max(1, int(seconds + 0.999))
    mins, rest = divmod(secs, 60)
    if mins:
        return f"{mins}м {rest}с" if rest else f"{mins}м"
    return f"{secs}с"


def skills_kb(user_id: int, ref: str = "") -> InlineKeyboardMarkup:
    """Три навыка в один ряд. Зелёная: готов (только эмодзи). Красная: перезарядка («🔥 · 7с»)."""
    bid = resolve(ref)
    user = D.get_user(user_id)
    rows = []
    if user is not None:
        row = []
        for i, sk in enumerate(skills_of(user)):
            left = cd_left(user, i)
            if left > 0:
                label, style = f"{sk['icon']} · {_short_dur(left)}", ButtonStyle.DANGER
            else:
                label, style = sk["icon"], ButtonStyle.SUCCESS
            row.append(_btn(label, f"boss:hit:{bid}:{i}", style=style))
        rows.append(row)
    rows.append(_back_row(f"boss:info:{bid}"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def top_kb(user_id: int, ref: str = "") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[_back_row("boss:open")])


# ---------- Отправка ----------

SCREENS = {
    "boss": (boss_html, boss_kb),
    "boss_list": (boss_list_html, list_kb),
    "boss_info": (boss_info_html, info_kb),
    "boss_skills": (skills_html, skills_kb),
    "boss_top": (top_html, top_kb),
}


async def send_screen(bot, chat_id: int, user_id: int, key: str, ref: str = ""):
    html, kb = SCREENS[key]
    return await D.send_rich_card(
        bot, chat_id, key, lambda with_image: html(user_id, with_image, ref), kb(user_id, ref)
    )


async def send_preview(bot, chat_id: int, user_id: int, key: str) -> None:
    """Предпросмотр для /img: без кнопок под сообщением."""
    html, _ = SCREENS[key]
    await D.send_rich_card(bot, chat_id, key, lambda with_image: html(user_id, with_image, ""))


async def _require_user(callback: CallbackQuery) -> dict | None:
    user = D.get_user(callback.from_user.id)
    if user is None:
        await callback.answer("Сначала выбери питомца: /start", show_alert=True)
    return user


# ---------- «Лайв»: окна боя обновляются, когда бьют другие ----------

# {user_id: {"chat": id, "msg": id, "bid": босс, "ts": время последнего действия}}
VIEWERS: dict[int, dict] = {}
_dirty: set[str] = set()   # у каких боссов что-то изменилось
_last_push = 0.0
_task: asyncio.Task | None = None


def _kb_sig(kb: InlineKeyboardMarkup) -> tuple:
    """Подпись клавиатуры (текст и цвет кнопок), чтобы не править её без изменений."""
    return tuple(tuple((b.text, str(b.style)) for b in row) for row in kb.inline_keyboard)


def _watch(user_id: int, chat_id: int, message_id: int, bid: str) -> None:
    VIEWERS[user_id] = {
        "chat": chat_id, "msg": message_id, "bid": bid, "ts": time.time(),
        "sig": _kb_sig(skills_kb(user_id, bid)),
    }


async def _edit_battle(bot, user_id: int, v: dict) -> bool:
    """Обновляет окно боя игрока на месте. False, если окно больше не обновить."""
    file_id = D.pet_images.get("boss_skills")
    kwargs = {"html": skills_html(user_id, bool(file_id), v["bid"])}
    if file_id:
        kwargs["media"] = [
            InputRichMessageMedia(id="pet", media=InputMediaPhoto(media=file_id))
        ]
    kb = skills_kb(user_id, v["bid"])
    try:
        await bot.edit_message_text(
            chat_id=v["chat"],
            message_id=v["msg"],
            rich_message=InputRichMessage(**kwargs),
            reply_markup=kb,
        )
        v["sig"] = _kb_sig(kb)
        return True
    except TelegramBadRequest as e:
        if "not modified" in str(e).lower():
            return True
        logging.info("Окно боя %s не обновить: %s", user_id, e)
        return False
    except Exception as e:
        logging.warning("Ошибка обновления окна боя %s: %s", user_id, e)
        return False


async def _refresh_buttons(bot, user_id: int, v: dict) -> tuple[bool, bool]:
    """Обновляет только кнопки навыков (откаты, цвет). Возвращает (окно живо, был ли запрос)."""
    kb = skills_kb(user_id, v["bid"])
    sig = _kb_sig(kb)
    if sig == v.get("sig"):
        return True, False
    try:
        await bot.edit_message_reply_markup(
            chat_id=v["chat"], message_id=v["msg"], reply_markup=kb
        )
        v["sig"] = sig
        return True, True
    except TelegramBadRequest as e:
        if "not modified" in str(e).lower():
            v["sig"] = sig
            return True, True
        logging.info("Кнопки боя %s не обновить: %s", user_id, e)
        return False, True
    except Exception as e:
        logging.warning("Ошибка обновления кнопок %s: %s", user_id, e)
        return False, True


async def _live_loop(bot) -> None:
    """Каждые KB_REFRESH секунд обновляет кнопки навыков, а целиком окна бойцов тех боссов,
    где что-то изменилось, не чаще раза в LIVE_INTERVAL секунд."""
    global _dirty, _last_push
    while True:
        await asyncio.sleep(KB_REFRESH)
        now = time.time()
        for uid, v in list(VIEWERS.items()):
            if now - v["ts"] > VIEW_TTL:
                VIEWERS.pop(uid, None)
        if not VIEWERS:
            return
        bids: set[str] = set()
        if _dirty and now - _last_push >= LIVE_INTERVAL:
            bids, _dirty = _dirty, set()
            _last_push = now
        for uid, v in list(VIEWERS.items()):
            if v["bid"] in bids:
                ok, called = await _edit_battle(bot, uid, v), True
            else:
                ok, called = await _refresh_buttons(bot, uid, v)
            if not ok:
                VIEWERS.pop(uid, None)
            if called:
                await asyncio.sleep(0.05)


_strike_task: asyncio.Task | None = None


async def _strike_loop(bot) -> None:
    """Раз в BOSS_STRIKE_INTERVAL секунд каждый босс бьёт по питомцам своих бойцов.
    Питомец получает не больше одного удара в минуту, даже если бьётся с несколькими боссами."""
    while True:
        await asyncio.sleep(BOSS_STRIKE_INTERVAL)
        try:
            tick()
            results: dict[str, bool | None] = {}  # uid -> увернулся? None: питомец отдыхает
            waves: list[str] = []
            for bid, b in BOSSES.items():
                if is_dead(bid) or not b["dmg"]:
                    continue
                hit = dodged = 0
                for uid in list(b["dmg"]):
                    if uid not in results:
                        user = D.get_user(int(uid))
                        if user is None or is_resting(user):
                            results[uid] = None  # питомец восстанавливается: босс его не трогает
                        else:
                            results[uid] = boss_strike(user)["dodge"]
                    r = results[uid]
                    if r is None:
                        continue
                    if r:
                        dodged += 1
                    else:
                        hit += 1
                if hit or dodged:
                    feed_push(bid, "wave", "", "", hit=hit, dodged=dodged)
                    waves.append(bid)
            if waves:
                D.save_users()
                save_boss()
                _mark_dirty(bot, *waves)
        except Exception as e:
            logging.warning("Ошибка удара боссов: %s", e)


_spawn_task: asyncio.Task | None = None


async def _spawn_loop(bot) -> None:
    """Следит за возрождением боссов и пишет тем, кто их бил (сложные и безумные)."""
    while True:
        await asyncio.sleep(SPAWN_CHECK)
        try:
            tick()
            while _spawned:
                bid, uids = _spawned.pop(0)
                c = cfg(bid)
                text = (
                    f"<b>{_boss_icon()} {escape(name_of(bid))} возродился!</b>\n\n"
                    f"<i>{c['icon']} {c['one']} босс снова в строю. "
                    "Ты уже сражался с ним, самое время вернуться.</i>"
                )
                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [_btn("К боссу", f"boss:info:{bid}", ATTACK_EMOJI[0], ButtonStyle.SUCCESS)]
                ])
                for uid in uids:
                    try:
                        await bot.send_message(int(uid), text, reply_markup=kb)
                    except Exception as e:  # игрок мог заблокировать бота
                        logging.warning("Не удалось сообщить о возрождении %s: %s", uid, e)
                    await asyncio.sleep(0.05)
        except Exception as e:
            logging.warning("Ошибка проверки возрождения: %s", e)


def start_strikes(bot) -> None:
    """Запускает фоновые задачи боссов: удары по питомцам и уведомления о возрождении.
    Можно вызвать при старте бота (boss.start_strikes(bot)), повторно безопасно."""
    global _strike_task, _spawn_task
    if _strike_task is None or _strike_task.done():
        _strike_task = asyncio.create_task(_strike_loop(bot))
    if _spawn_task is None or _spawn_task.done():
        _spawn_task = asyncio.create_task(_spawn_loop(bot))


def _mark_dirty(bot, *bids: str) -> None:
    global _task
    _dirty.update(bids)
    if _task is None or _task.done():
        _task = asyncio.create_task(_live_loop(bot))


async def _show(callback: CallbackQuery, key: str, ref: str = "") -> None:
    """Старое сообщение удаляем, присылаем новое."""
    uid = callback.from_user.id
    try:
        await callback.message.delete()
    except Exception as e:
        logging.warning("Не удалось удалить сообщение: %s", e)
    VIEWERS.pop(uid, None)
    msg = await send_screen(callback.bot, callback.message.chat.id, uid, key, ref)
    if key == "boss_skills" and msg is not None:
        _watch(uid, msg.chat.id, msg.message_id, resolve(ref))


def _bid_from(data: str) -> str | None:
    """Достаёт id босса («normal:3») из callback_data вида boss:действие:normal:3[:...]."""
    parts = data.split(":")
    bid = ":".join(parts[2:4])
    return bid if bid in BOSSES else None


# ---------- Хендлеры ----------

_TYPES_RE = "|".join(BOSS_TYPES)


@router.callback_query(F.data.in_({"menu:boss", "boss:open"}))
async def on_open(callback: CallbackQuery):
    if await _require_user(callback) is None:
        return
    await callback.answer()
    start_strikes(callback.bot)
    await _show(callback, "boss")


@router.callback_query(F.data.regexp(rf"^boss:list:({_TYPES_RE})$"))
async def on_list(callback: CallbackQuery):
    if await _require_user(callback) is None:
        return
    await callback.answer()
    await _show(callback, "boss_list", callback.data.split(":")[2])


@router.callback_query(F.data.regexp(r"^boss:info:\w+:\d+$"))
async def on_info(callback: CallbackQuery):
    if await _require_user(callback) is None:
        return
    bid = _bid_from(callback.data)
    if bid is None:
        await callback.answer("Такого босса нет", show_alert=True)
        return
    await callback.answer()
    await _show(callback, "boss_info", bid)


@router.callback_query(F.data.regexp(r"^boss:skills:\w+:\d+$"))
async def on_skills(callback: CallbackQuery):
    if await _require_user(callback) is None:
        return
    bid = _bid_from(callback.data)
    if bid is None:
        await callback.answer("Такого босса нет", show_alert=True)
        return
    await callback.answer()
    start_strikes(callback.bot)
    await _show(callback, "boss_skills", bid)


@router.callback_query(F.data.regexp(r"^boss:top(:\w+:\d+)?$"))
async def on_top(callback: CallbackQuery):
    """Общий топ бойцов (кнопки со старым id босса тоже ведут сюда)."""
    if await _require_user(callback) is None:
        return
    await callback.answer()
    await _show(callback, "boss_top")


@router.callback_query(F.data.regexp(r"^boss:skills$|^boss:hit:\d+$"))
async def on_old_buttons(callback: CallbackQuery):
    """Кнопки из старых сообщений (когда босс был один)."""
    await callback.answer("Окно устарело. Открой раздел «Босс» заново.", show_alert=True)


@router.callback_query(F.data.regexp(r"^pskills:(prof|sel:\w+)$"))
async def on_pet_skills(callback: CallbackQuery):
    """Кнопка «Навыки» на карточке питомца (выбор) или в профиле."""
    parts = callback.data.split(":")
    if parts[1] == "prof":
        user = await _require_user(callback)
        if user is None:
            return
        pet_key, back = user["pet"], "menu:pet"
    else:
        pet_key = parts[2]
        if pet_key not in PET_SKILLS:
            await callback.answer("Неизвестный питомец", show_alert=True)
            return
        back = f"pet:view:{pet_key}"

    await callback.answer()
    try:
        await callback.message.delete()
    except Exception as e:
        logging.warning("Не удалось удалить сообщение: %s", e)
    await D.send_rich_card(
        callback.bot, callback.message.chat.id, pet_key,
        lambda with_image: pet_skills_html(pet_key, with_image),
        InlineKeyboardMarkup(inline_keyboard=[_back_row(back)]),
    )


async def _notify_rewards(bot, bid: str, rewards: dict[str, dict[str, int]], killer: str) -> None:
    for uid, r in rewards.items():
        text = (
            f"<b>{_boss_icon()} {escape(name_of(bid))} повержен!</b>\n\n"
            f"<i>Последний удар нанёс {escape(killer)}.</i>\n"
            f"<b>Твоя награда:</b> {_coin()} {fmt(r['coins'])}  "
            f"🥩 {r['meat']}  🥭 {r['fruit']}"
        )
        if r["last"]:
            text += "\n<i>В том числе бонус за последний удар.</i>"
        try:
            await bot.send_message(int(uid), text)
        except Exception as e:  # игрок мог заблокировать бота
            logging.warning("Не удалось отправить награду %s: %s", uid, e)
        await asyncio.sleep(0.05)


@router.callback_query(F.data.regexp(r"^boss:hit:\w+:\d+:\d+$"))
async def on_hit(callback: CallbackQuery):
    user = await _require_user(callback)
    if user is None:
        return
    bid = _bid_from(callback.data)
    if bid is None:
        await callback.answer("Такого босса нет", show_alert=True)
        return
    tick()
    b = BOSSES[bid]

    skills = skills_of(user)
    i = int(callback.data.split(":")[4])
    if not 0 <= i < len(skills):
        await callback.answer()
        return
    skill = skills[i]

    if is_dead(bid):
        await callback.answer(
            f"Босс повержен. Вернётся через {fmt_dur(b['dead_until'] - time.time())}.",
            show_alert=True,
        )
        return

    wait = pet_ready_in(user)
    if wait > 0:
        await callback.answer(
            f"{D.PETS[user['pet']]['name']} отдыхает и не может атаковать. "
            f"Вернётся в бой через {fmt_dur(wait)}.",
            show_alert=True,
        )
        return

    left = cd_left(user, i)
    if left > 0:
        await callback.answer(
            f"{skill['name']} перезаряжается: {fmt_dur(left)}", show_alert=True
        )
        return

    total, hits = roll_attack(user, skill, bid)
    dealt = min(total, b["hp"])
    uid = str(callback.from_user.id)

    b["hp"] -= dealt
    b["dmg"][uid] = int(b["dmg"].get(uid, 0)) + dealt
    b["names"][uid] = nick_of(callback.from_user)
    b["last_uid"] = uid
    t = TOTALS.setdefault(uid, {"dmg": 0, "name": b["names"][uid]})
    t["dmg"] += dealt
    t["name"] = b["names"][uid]
    user.setdefault("boss_cd", {})[str(i)] = time.time()

    crits = sum(1 for _, c in hits if c)
    text = f"{skill['name']} · {fmt(dealt)} урона"
    if crits:
        text += f" · крит ×{crits}" if len(hits) > 1 else " · КРИТ"

    name = b["names"][uid]
    feed_push(bid, "hit", uid, name, icon=skill["icon"], skill=skill["name"],
              dmg=dealt, crit=crits > 0)

    if b["hp"] <= 0:
        b["hp"] = 0
        feed_push(bid, "kill", uid, name)
        rewards = finish_boss(bid, name)
        await callback.answer(text + ". Босс повержен!", show_alert=True)
        asyncio.create_task(_notify_rewards(callback.bot, bid, rewards, name))
    else:
        D.save_users()
        save_boss()
        await callback.answer(text)

    start_strikes(callback.bot)
    # Своё окно обновляем сразу на месте, окна остальных бойцов через общий цикл
    _watch(callback.from_user.id, callback.message.chat.id, callback.message.message_id, bid)
    await _edit_battle(callback.bot, callback.from_user.id,
                       VIEWERS[callback.from_user.id])
    _mark_dirty(callback.bot, bid)
