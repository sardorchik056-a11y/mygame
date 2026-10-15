"""Раздел «Босс»: один общий босс на всех игроков.

Модуль самостоятельный, как mine.py: main.py не импортирует, нужные функции
передаются один раз через boss.setup(...).

Как это работает:
  * Босс один на весь бот, у него BOSS_MAX_HP здоровья. Бьют его все игроки,
    урон каждого записывается (файл boss.json рядом с ботом).
  * У каждого питомца 3 врождённых навыка (PET_SKILLS). У навыка свой откат,
    он считается у каждого игрока отдельно.
  * Урон = атака питомца (с учётом уровня) × множитель навыка, есть шанс крита
    (зависит от удачи). Часть навыков пробивает защиту босса.
  * Когда босс падает, награду получают все, кто его бил, пропорционально
    нанесённому урону. Тот, кто добил, получает ещё и бонус.
  * Через RESPAWN_MIN минут босс возрождается с полным здоровьем.
"""

import asyncio
import json
import logging
import random
import time
from html import escape
from pathlib import Path
from types import SimpleNamespace

from aiogram import F, Router
from aiogram.enums import ButtonStyle
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

router = Router()

# Заполняется в setup()
D = SimpleNamespace()


def setup(**deps) -> None:
    """Передаём модулю функции и данные из main.py.

    Нужны: get_user, save_users, send_rich_card, custom_emoji, COIN_EMOJI,
    INFO_EMOJI, BACK_EMOJI_ID, PETS, pet_stats.
    """
    for name, value in deps.items():
        setattr(D, name, value)


# ---------- Баланс ----------

BOSS_NAME = "Ксарвог, Пожиратель Миров"
BOSS_STORY = (
    "Ксарвог выполз из трещины между мирами и с тех пор не знает сна. "
    "Он питается силой чужих питомцев, а его шкура крепче любой брони. "
    "Один он неуязвим, но все вместе вы справитесь."
)
BOSS_SHORT = "Ксарвог"
BOSS_EPITHET = "Пожиратель Миров"
BOSS_ICON = "👹"

BOSS_MAX_HP = 100_000
BOSS_DEF = 20            # обычный урон делится на (1 + защита/100); «пробивающие» навыки её игнорируют
CRIT_MULT = 2.0          # во сколько раз крит сильнее
CRIT_MAX = 0.6           # шанс крита не выше
RESPAWN_MIN = 60         # через сколько минут после смерти босс возрождается

# Награда за убийство делится между всеми по доле нанесённого урона
REWARD_COINS = 50_000
REWARD_MEAT = 300
REWARD_FRUIT = 300
# Бонус тому, кто нанёс последний удар
LAST_HIT_COINS = 5_000
LAST_HIT_MEAT = 20
LAST_HIT_FRUIT = 20

TOP_SIZE = 10

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
    "boss": "Босс",
    "boss_skills": "Босс: навыки",
    "boss_top": "Босс: топ игроков",
}


# ---------- Состояние босса (общее для всех) ----------

BOSS_FILE = Path(__file__).with_name("boss.json")


def _fresh(kills: int = 0) -> dict:
    return {
        "hp": BOSS_MAX_HP,
        "dmg": {},           # {"user_id": нанесённый урон}
        "names": {},         # {"user_id": имя для топа}
        "dead_until": None,  # когда возродится, если босс убит
        "kills": kills,      # сколько раз босса уже убивали
        "killer": None,      # имя того, кто добил в последний раз
    }


def _load() -> dict:
    try:
        data = json.loads(BOSS_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return _fresh()
    base = _fresh()
    base.update(data)
    return base


boss: dict = _load()


def save_boss() -> None:
    BOSS_FILE.write_text(json.dumps(boss, ensure_ascii=False, indent=2), encoding="utf-8")


def is_dead() -> bool:
    return boss["dead_until"] is not None


def tick() -> None:
    """Возродить босса, если время пришло."""
    if is_dead() and time.time() >= boss["dead_until"]:
        kills = boss["kills"]
        boss.clear()
        boss.update(_fresh(kills))
        save_boss()


def ranking() -> list[tuple[str, int]]:
    return sorted(boss["dmg"].items(), key=lambda kv: kv[1], reverse=True)


def place_of(uid: str) -> int | None:
    for n, (u, _) in enumerate(ranking(), start=1):
        if u == uid:
            return n
    return None


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
        return f"{mins} мин {secs} сек"
    return f"{secs} сек"


def hp_ratio() -> float:
    return max(0.0, boss["hp"] / BOSS_MAX_HP)


def hp_bar(width: int = 10) -> str:
    """Цветная шкала: зелёная, жёлтая, красная в зависимости от остатка."""
    ratio = hp_ratio()
    filled = 0 if boss["hp"] <= 0 else max(1, round(ratio * width))
    color = "🟩" if ratio > 0.5 else "🟨" if ratio > 0.25 else "🟥"
    return color * filled + "⬛" * (width - filled)


def skill_tag(sk: dict) -> str:
    """Короткая сводка навыка для таблицы: «4 × 0.6», «1 × 1.4 · пробой»."""
    tag = f"{sk['hits']} × {sk['mult']:g}"
    if sk.get("pierce"):
        tag += " · пробой"
    if sk.get("crit"):
        tag += " · крит"
    if sk.get("pct"):
        tag += f" · +{round(sk['pct'] * 100)}% HP"
    return tag


def skills_of(user: dict) -> list[dict]:
    return PET_SKILLS[user["pet"]]


def cd_left(user: dict, i: int) -> float:
    last = user.get("boss_cd", {}).get(str(i), 0)
    return max(0.0, last + skills_of(user)[i]["cd"] - time.time())


def roll_attack(user: dict, skill: dict) -> tuple[int, list[tuple[int, bool]]]:
    """Считает урон навыка. Возвращает (общий урон, [(урон удара, крит)])."""
    stats = D.pet_stats(D.PETS[user["pet"]], user["level"])
    atk, luck = stats["atk"], stats["luck"]
    crit_chance = min(CRIT_MAX, luck / 400 + skill.get("crit", 0.0))

    hits: list[tuple[int, bool]] = []
    for _ in range(skill["hits"]):
        dmg = atk * skill["mult"] * random.uniform(0.9, 1.1)
        if not skill.get("pierce"):
            dmg *= 100 / (100 + BOSS_DEF)
        crit = random.random() < crit_chance
        if crit:
            dmg *= CRIT_MULT
        hits.append((max(1, round(dmg)), crit))

    total = sum(d for d, _ in hits)
    if skill.get("pct"):
        total += min(round(boss["hp"] * skill["pct"]), round(atk * 4))
    return total, hits


def finish_boss(killer_name: str) -> dict[str, dict[str, int]]:
    """Босс убит: выдаём награды, ставим время возрождения.
    Возвращает {user_id: {"coins":..., "meat":..., "fruit":..., "last": 0/1}}."""
    total = sum(boss["dmg"].values()) or 1
    rewards: dict[str, dict[str, int]] = {}
    last_uid = boss.get("last_uid")

    for uid, dmg in boss["dmg"].items():
        user = D.get_user(int(uid))
        if user is None:
            continue
        share = dmg / total
        r = {
            "coins": max(1, round(REWARD_COINS * share)),
            "meat": max(1, round(REWARD_MEAT * share)),
            "fruit": max(1, round(REWARD_FRUIT * share)),
            "last": 0,
        }
        if uid == last_uid:
            r["coins"] += LAST_HIT_COINS
            r["meat"] += LAST_HIT_MEAT
            r["fruit"] += LAST_HIT_FRUIT
            r["last"] = 1
        user["coins"] += r["coins"]
        user["meat"] += r["meat"]
        user["fruit"] += r["fruit"]
        rewards[uid] = r

    boss["dead_until"] = time.time() + RESPAWN_MIN * 60
    boss["kills"] += 1
    boss["killer"] = killer_name
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


def _button(data: str, label: str, emoji: str = "", style: str = "success") -> str:
    text = f"{emoji} {label}".strip()
    return (
        "<tg-button-row>"
        f'<tg-button type="callback_data" data="{data}" '
        f'style="{style}">{text}</tg-button>'
        "</tg-button-row>"
    )


def _info() -> str:
    return D.custom_emoji(D.INFO_EMOJI)


def _coin() -> str:
    return D.custom_emoji(D.COIN_EMOJI)


def _image(with_image: bool) -> str:
    return '<img src="tg://photo?id=pet"/>' if with_image else ""


def skills_block(pet_key: str) -> str:
    """Таблица навыков питомца: для карточек питомцев в main.py."""
    rows = [
        (f"{s['icon']} {escape(s['name'])}", escape(s["desc"]))
        for s in PET_SKILLS[pet_key]
    ]
    return "<p><b>Врождённые навыки</b></p>" + _table(rows, head=("Навык", "Описание"))


# ---------- Экраны ----------

# Результат последней атаки каждого игрока (показывается в окне боя)
LAST_RESULT: dict[str, str] = {}


def _head(sub: str = "") -> str:
    """Шапка как у карточек питомцев: имя · статус, пустая строка, подзаголовок."""
    return (
        f"<p><b>{escape(BOSS_SHORT.upper())} · МИРОВОЙ БОСС</b>"
        + (f"<br>&nbsp;<br>{sub}" if sub else "")
        + "</p>"
    )


def boss_html(user_id: int, with_image: bool = False) -> str:
    """Первое окно: кто это, как всё устроено и что можно получить."""
    tick()
    how = [
        ("Босс", "один на всех"),
        ("Урон", "общий, считается каждому"),
        ("Награда", "по доле твоего урона"),
        ("Возрождение", f"через {RESPAWN_MIN} мин"),
    ]
    reward = [
        (f"{_coin()} Монеты", fmt(REWARD_COINS)),
        ("🥩 Мясо", fmt(REWARD_MEAT)),
        ("🥭 Фрукты", fmt(REWARD_FRUIT)),
        ("🎯 Последний удар",
         f"+{fmt(LAST_HIT_COINS)} · +{LAST_HIT_MEAT} · +{LAST_HIT_FRUIT}"),
    ]
    return (
        f"{_image(with_image)}"
        + _head(f"<b>{escape(BOSS_EPITHET)}</b>")
        + f"<blockquote><i>{escape(BOSS_STORY)}</i></blockquote>"
        + "<p><b>Как это работает</b></p>"
        + _table(how)
        + "<p><b>Награда за победу</b></p>"
        + _table(reward, head=("Ресурс", "Всего на всех"))
        + _button("boss:skills", "Атаковать", "⚔️", "danger")
    )


def skills_html(user_id: int, with_image: bool = False) -> str:
    """Окно боя: здоровье босса, твоя статистика и навыки питомца."""
    tick()
    uid = str(user_id)
    user = D.get_user(user_id)
    place = place_of(uid)

    if is_dead():
        sub = (
            f"<b>☠️ Повержен</b><br>"
            f"<i>Вернётся через {fmt_dur(boss['dead_until'] - time.time())}</i>"
        )
    else:
        sub = (
            f"<b>❤️ {fmt(boss['hp'])} / {fmt(BOSS_MAX_HP)}</b><br>"
            f"<b>{hp_bar()} {hp_ratio() * 100:.1f}%</b>"
        )

    stats = [
        ("Бойцов", str(len(boss["dmg"]))),
        ("Твой урон", fmt(boss["dmg"].get(uid, 0))),
        ("Твоё место", str(place) if place else "—"),
    ]

    last = LAST_RESULT.get(uid)
    last_part = (
        f"<blockquote><i>Последняя атака: {escape(last)}</i></blockquote>" if last else ""
    )

    skills_part = ""
    if user is not None:
        rows = []
        for i, sk in enumerate(skills_of(user)):
            left = cd_left(user, i)
            rows.append((
                f"{sk['icon']} {escape(sk['name'])}",
                escape(skill_tag(sk)),
                "✅ готов" if left <= 0 else f"⏳ {fmt_dur(left)}",
            ))
        body = "".join(
            "<tr>"
            f"<td><b>{n}</b></td>"
            f'<td align="center"><b>{t}</b></td>'
            f'<td align="center"><b>{st}</b></td>'
            "</tr>"
            for n, t, st in rows
        )
        skills_part = (
            "<p><b>Навыки питомца</b></p>"
            "<table bordered striped>"
            "<tr><th><b>Навык</b></th><th><b>Удар</b></th><th><b>Статус</b></th></tr>"
            f"{body}"
            "</table>"
            "<p><i>Удар: сколько раз × множитель атаки. «Пробой» игнорирует защиту босса.</i></p>"
        )

    return (
        f"{_image(with_image)}"
        + _head(sub)
        + last_part
        + _table(stats)
        + skills_part
    )


def top_html(user_id: int, with_image: bool = False) -> str:
    tick()
    uid = str(user_id)
    rank = ranking()
    total = sum(boss["dmg"].values()) or 1

    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    rows = [
        (
            f"{medals.get(n, str(n) + '.')} "
            f"{escape(str(boss['names'].get(u, 'Игрок')))}",
            f"{fmt(d)} · {d / total * 100:.1f}%",
        )
        for n, (u, d) in enumerate(rank[:TOP_SIZE], start=1)
    ]
    me = place_of(uid)
    if rows:
        body = _table(rows, head=("Игрок", "Урон · доля"))
    else:
        body = "<p><i>Пока никто не ударил босса. Стань первым.</i></p>"
    tail = (
        f"<i>Твоё место: {me} · урон {fmt(boss['dmg'][uid])}</i>"
        if me else "<i>Ты ещё не бил босса.</i>"
    )
    return (
        f"{_image(with_image)}"
        + "<p><b>ТОП БОЙЦОВ</b><br>&nbsp;<br>"
        + f"<b>{escape(BOSS_SHORT)} · {fmt(boss['hp'])} / {fmt(BOSS_MAX_HP)}</b></p>"
        + body
        + f"<p>{tail}</p>"
    )


# ---------- Клавиатуры ----------

def _btn(text: str, data: str, emoji_id: str | None = None,
         style: ButtonStyle = ButtonStyle.PRIMARY) -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text=text, callback_data=data, style=style, icon_custom_emoji_id=emoji_id
    )


def _back_row(data: str) -> list[InlineKeyboardButton]:
    return [_btn("Назад", data, D.BACK_EMOJI_ID)]


def boss_kb(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_btn("🏆 Топ бойцов", "boss:top")],
            _back_row("menu:back"),
        ]
    )


def skills_kb(user_id: int) -> InlineKeyboardMarkup:
    user = D.get_user(user_id)
    rows = []
    if user is not None:
        for i, sk in enumerate(skills_of(user)):
            left = cd_left(user, i)
            if left > 0:
                label, style = f"⏳ {sk['name']} · {fmt_dur(left)}", ButtonStyle.PRIMARY
            else:
                label, style = f"{sk['icon']} {sk['name']}", ButtonStyle.DANGER
            rows.append([_btn(label, f"boss:hit:{i}", style=style)])
    rows.append([_btn("🏆 Топ", "boss:top"), _btn("🔄 Обновить", "boss:skills")])
    rows.append(_back_row("boss:open"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def top_kb(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[_back_row("boss:open")])


# ---------- Отправка ----------

SCREENS = {
    "boss": (boss_html, boss_kb),
    "boss_skills": (skills_html, skills_kb),
    "boss_top": (top_html, top_kb),
}


async def send_screen(bot, chat_id: int, user_id: int, key: str) -> None:
    html, kb = SCREENS[key]
    await D.send_rich_card(
        bot, chat_id, key, lambda with_image: html(user_id, with_image), kb(user_id)
    )


async def send_preview(bot, chat_id: int, user_id: int, key: str) -> None:
    """Предпросмотр для /img: без кнопок под сообщением."""
    html, _ = SCREENS[key]
    await D.send_rich_card(bot, chat_id, key, lambda with_image: html(user_id, with_image))


async def _require_user(callback: CallbackQuery) -> dict | None:
    user = D.get_user(callback.from_user.id)
    if user is None:
        await callback.answer("Сначала выбери питомца: /start", show_alert=True)
    return user


async def _show(callback: CallbackQuery, key: str) -> None:
    """Старое сообщение удаляем, присылаем новое."""
    try:
        await callback.message.delete()
    except Exception as e:
        logging.warning("Не удалось удалить сообщение: %s", e)
    await send_screen(callback.bot, callback.message.chat.id, callback.from_user.id, key)


# ---------- Хендлеры ----------

@router.callback_query(F.data.in_({"menu:boss", "boss:open"}))
async def on_open(callback: CallbackQuery):
    if await _require_user(callback) is None:
        return
    await callback.answer()
    await _show(callback, "boss")


@router.callback_query(F.data == "boss:skills")
async def on_skills(callback: CallbackQuery):
    if await _require_user(callback) is None:
        return
    await callback.answer()
    await _show(callback, "boss_skills")


@router.callback_query(F.data == "boss:top")
async def on_top(callback: CallbackQuery):
    if await _require_user(callback) is None:
        return
    await callback.answer()
    await _show(callback, "boss_top")


async def _notify_rewards(bot, rewards: dict[str, dict[str, int]], killer: str) -> None:
    for uid, r in rewards.items():
        text = (
            f"<b>{BOSS_ICON} {escape(BOSS_SHORT)} повержен!</b>\n\n"
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


@router.callback_query(F.data.regexp(r"^boss:hit:\d+$"))
async def on_hit(callback: CallbackQuery):
    user = await _require_user(callback)
    if user is None:
        return
    tick()

    skills = skills_of(user)
    i = int(callback.data.split(":")[2])
    if not 0 <= i < len(skills):
        await callback.answer()
        return
    skill = skills[i]

    if is_dead():
        await callback.answer(
            f"Босс повержен. Вернётся через {fmt_dur(boss['dead_until'] - time.time())}.",
            show_alert=True,
        )
        return

    left = cd_left(user, i)
    if left > 0:
        await callback.answer(
            f"{skill['name']} перезаряжается: {fmt_dur(left)}", show_alert=True
        )
        return

    total, hits = roll_attack(user, skill)
    dealt = min(total, boss["hp"])
    uid = str(callback.from_user.id)

    boss["hp"] -= dealt
    boss["dmg"][uid] = int(boss["dmg"].get(uid, 0)) + dealt
    boss["names"][uid] = callback.from_user.first_name or "Игрок"
    boss["last_uid"] = uid
    user.setdefault("boss_cd", {})[str(i)] = time.time()

    crits = sum(1 for _, c in hits if c)
    text = f"{skill['name']} · {fmt(dealt)} урона"
    if crits:
        text += f" · крит ×{crits}" if len(hits) > 1 else " · КРИТ"
    LAST_RESULT[uid] = text

    if boss["hp"] <= 0:
        boss["hp"] = 0
        rewards = finish_boss(boss["names"][uid])
        await callback.answer(text + ". Босс повержен!", show_alert=True)
        asyncio.create_task(_notify_rewards(callback.bot, rewards, boss["names"][uid]))
    else:
        D.save_users()
        save_boss()
        await callback.answer(text)

    await _show(callback, "boss_skills")
