"""Снаряжение питомца: до трёх артефактов, каждый усиливает определённую вещь.

Модуль самостоятельный, как mine.py, boss.py и cases.py: main.py он не импортирует,
нужные функции передаются один раз через equipment.setup(...).

Как это работает:
  * У питомца 3 слота (SLOTS). Один артефакт можно надеть только один раз.
  * Артефактов 10 (ARTIFACTS). Каждый даёт бонус в процентах: к характеристикам
    питомца (урон, здоровье, скорость, удача) или к боевым умениям (быстрее
    перезарядка навыков, быстрее восстановление HP после ударов босса).
  * Бонусы складываются. user_stats(user) отдаёт характеристики с учётом артефактов,
    bonus(user, вид) отдаёт сумму бонуса одного вида в процентах.
  * Надетое лежит у игрока в user["equipment"] (список ключей артефактов).
  * Пока артефакты не выдаются кейсами, надеть можно любой из десяти.
"""

import logging
from types import SimpleNamespace

from aiogram import F, Router
from aiogram.enums import ButtonStyle
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

router = Router()

# Заполняется в setup()
D = SimpleNamespace()


def setup(**deps) -> None:
    """Передаём модулю функции и эмодзи из main.py.

    Нужны: get_user, save_users, send_rich_card, custom_emoji, INFO_EMOJI,
    BACK_EMOJI_ID, PETS, pet_stats, STAT_EMOJI.
    """
    for name, value in deps.items():
        setattr(D, name, value)


# ---------- Баланс ----------

SLOTS = 3   # сколько артефактов можно надеть

# Виды бонусов: ключ -> (название, знак, эмодзи).
# Эмодзи None: берём кастомный из STAT_EMOJI (так у характеристик). Строка: обычный эмодзи.
# atk, hp, spd, luck увеличивают характеристику питомца; cd и regen это боевые умения.
BONUS_KINDS: dict[str, tuple[str, str, str | tuple | None]] = {
    "atk": ("Урон", "+", None),
    "hp": ("Здоровье", "+", None),
    "spd": ("Скорость", "+", None),
    "luck": ("Удача", "+", None),
    "cd": ("Перезарядка навыков", "−", "⏱"),
    "regen": ("Восстановление HP", "+", "💚"),
}
STAT_KEYS = ("hp", "atk", "def", "spd", "luck")
MAX_CD_CUT = 50   # перезарядка навыков не короче половины

# Артефакты: ключ, название, значок, класс, бонус {вид: проценты}, описание.
# emoji: необязательный кастомный эмодзи (id, запасной значок), если задан,
# показывается вместо icon.
ARTIFACTS = [
    {"key": "ring", "name": "Кольцо ярости", "icon": "💍", "tier": "Обычный",
     "bonus": {"atk": 10},
     "desc": "Тёплое на ощупь. Питомец в нём бьёт заметно злее."},
    {"key": "amulet", "name": "Амулет жизни", "icon": "🧿", "tier": "Обычный",
     "bonus": {"hp": 10},
     "desc": "Хранит чужую удачу и немного чужого здоровья."},
    {"key": "boots", "name": "Сапоги ветра", "icon": "👢", "tier": "Обычный",
     "bonus": {"spd": 10},
     "desc": "Лёгкие, будто ничего не весят. В них проще увернуться."},
    {"key": "clover", "name": "Клевер удачи", "icon": "🍀", "tier": "Обычный",
     "bonus": {"luck": 10},
     "desc": "Четырёхлистный, и листья всегда свежие, сколько бы он ни пролежал."},
    {"key": "hourglass", "name": "Песочные часы", "icon": "⌛", "tier": "Обычный",
     "bonus": {"cd": 10},
     "desc": "Песок в них течёт чуть быстрее, чем положено. Навыки восстанавливаются раньше."},
    {"key": "fang", "name": "Клык хищника", "icon": "🦷", "tier": "Редкий",
     "bonus": {"atk": 20},
     "desc": "Когда-то принадлежал тому, кто никогда не промахивался."},
    {"key": "heart", "name": "Сердце титана", "icon": "❤️", "tier": "Редкий",
     "bonus": {"hp": 20},
     "desc": "Бьётся медленно и гулко. Рядом с ним питомец становится выносливее."},
    {"key": "wings", "name": "Крылья ветра", "icon": "🕊️", "tier": "Редкий",
     "bonus": {"spd": 20},
     "desc": "Невесомые перья, которые подхватывает любой сквозняк."},
    {"key": "eye", "name": "Око судьбы", "icon": "👁️", "tier": "Редкий",
     "bonus": {"luck": 20},
     "desc": "Смотрит на тебя в ответ и, кажется, знает, куда ты ударишь."},
    {"key": "feather", "name": "Перо феникса", "icon": "🪶", "tier": "Редкий",
     "bonus": {"regen": 20},
     "desc": "Тлеет, но не сгорает. Раны под ним затягиваются быстрее."},
]
BY_KEY = {a["key"]: a for a in ARTIFACTS}

# Куда админ может добавить фото
IMG_TARGETS: dict[str, str] = {
    "equip": "Снаряжение",
    "equip_list": "Снаряжение: список артефактов",
    "equip_item": "Снаряжение: карточка артефакта",
}


# ---------- Логика ----------

def equipped(user: dict) -> list[dict]:
    """Надетые артефакты по порядку слотов (неизвестные ключи пропускаем)."""
    return [BY_KEY[k] for k in user.get("equipment", []) if k in BY_KEY]


def bonus(user: dict, kind: str) -> int:
    """Сумма бонуса одного вида в процентах от всех надетых артефактов."""
    total = sum(a["bonus"].get(kind, 0) for a in equipped(user))
    return min(total, MAX_CD_CUT) if kind == "cd" else total


def user_stats(user: dict) -> dict[str, int]:
    """Характеристики питомца с учётом артефактов."""
    base = D.pet_stats(D.PETS[user["pet"]], user["level"])
    return {
        key: round(val * (1 + bonus(user, key) / 100)) if key in STAT_KEYS else val
        for key, val in base.items()
    }


def fmt_bonus(kind: str, pct: int) -> str:
    return f"{BONUS_KINDS[kind][1]}{pct}%"


# ---------- Оформление ----------

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


def _image(with_image: bool) -> str:
    return '<img src="tg://photo?id=pet"/>' if with_image else ""


def _e(pair: tuple[str, str]) -> str:
    return D.custom_emoji(pair)


def _icon(a: dict) -> str:
    """Значок артефакта в тексте: кастомный, если задан, иначе обычный."""
    return D.custom_emoji(a["emoji"]) if a.get("emoji") else a["icon"]


def _kind_icon(kind: str) -> str:
    """Эмодзи вида бонуса: для характеристик из STAT_EMOJI, для умений свой."""
    icon = BONUS_KINDS[kind][2]
    if isinstance(icon, str):
        return icon
    return D.custom_emoji(icon or D.STAT_EMOJI[kind])


def _bonus_rows(bonuses: dict[str, int]) -> list[tuple[str, str]]:
    return [
        (f"{_kind_icon(kind)} {BONUS_KINDS[kind][0]}", fmt_bonus(kind, pct))
        for kind, pct in bonuses.items()
    ]


# ---------- Экраны ----------

def equip_html(user_id: int, with_image: bool = False) -> str:
    """Главное окно снаряжения: слоты и суммарные бонусы."""
    user = D.get_user(user_id)
    worn = equipped(user)
    pet_name = D.PETS[user["pet"]]["name"]

    slots = []
    for n in range(SLOTS):
        if n < len(worn):
            a = worn[n]
            slots.append((f"{n + 1} слот", f"{_icon(a)} {a['name']}", _bonus_rows(a["bonus"])))
        else:
            slots.append((f"{n + 1} слот", "пусто"))

    total: dict[str, int] = {}
    for kind in BONUS_KINDS:
        pct = bonus(user, kind)
        if pct:
            total[kind] = pct
    summary = (
        _tree(_bonus_rows(total), "Общие бонусы")
        if total else _tree([("Артефакты не надеты", "")], "Общие бонусы")
    )

    return (
        f"{_image(with_image)}"
        f"<p><b>🎒 СНАРЯЖЕНИЕ</b><br>&nbsp;<br>"
        f"<b>{pet_name.upper()} · занято {len(worn)}/{SLOTS}</b></p>"
        "<blockquote><i>Артефакты усиливают питомца. Надень до трёх, "
        "и их бонусы сложатся.</i></blockquote>"
        + _tree(slots, "Слоты")
        + _gap()
        + summary
    )


def list_html(user_id: int, with_image: bool = False) -> str:
    """Все артефакты по классам; надетые отмечены галочкой."""
    user = D.get_user(user_id)
    worn_keys = {a["key"] for a in equipped(user)}

    titles = {"Обычный": "Обычные артефакты", "Редкий": "Редкие артефакты"}
    blocks = []
    for tier in titles:
        rows = []
        for a in ARTIFACTS:
            if a["tier"] != tier:
                continue
            kind, pct = next(iter(a["bonus"].items()))
            mark = " ✅" if a["key"] in worn_keys else ""
            rows.append((f"{_icon(a)} {a['name']}{mark}", f"{BONUS_KINDS[kind][0]} {fmt_bonus(kind, pct)}"))
        blocks.append(_tree(rows, titles[tier]))

    return (
        f"{_image(with_image)}"
        "<p><b>🎒 АРТЕФАКТЫ</b><br>&nbsp;<br>"
        f"<b>Выбери артефакт, чтобы надеть или снять</b></p>"
        + _gap().join(blocks)
    )


def item_html(user_id: int, key: str, with_image: bool = False) -> str:
    """Карточка артефакта: что даёт и надет ли он."""
    a = BY_KEY.get(key, ARTIFACTS[0])
    user = D.get_user(user_id)
    worn_keys = [x["key"] for x in equipped(user)]
    is_worn = a["key"] in worn_keys

    state = [
        ("🔓 Статус", "надет" if is_worn else "не надет"),
        ("🎒 Слоты", f"{len(worn_keys)}/{SLOTS} заняты"),
    ]
    return (
        f"{_image(with_image)}"
        f"<p><b>{_icon(a)} {a['name'].upper()}</b><br>&nbsp;<br><b>{a['tier']} артефакт</b></p>"
        f"<blockquote><i>{a['desc']}</i></blockquote>"
        + _tree(_bonus_rows(a["bonus"]), "Бонус")
        + _gap()
        + _tree(state, "Состояние")
    )


# ---------- Клавиатуры ----------

def _btn(text: str, data: str, emoji_id: str | None = None,
         style: ButtonStyle = ButtonStyle.SUCCESS) -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text=text,
        callback_data=data,
        style=style,
        icon_custom_emoji_id=emoji_id,
    )


def _back_row(data: str) -> list[InlineKeyboardButton]:
    return [_btn("Назад", data, D.BACK_EMOJI_ID, ButtonStyle.PRIMARY)]


def _artifact_btn(a: dict, text: str, data: str, style: ButtonStyle) -> InlineKeyboardButton:
    """Кнопка артефакта: кастомный эмодзи ставится иконкой кнопки."""
    if a.get("emoji"):
        return _btn(text, data, a["emoji"][0], style)
    return _btn(f"{a['icon']} {text}".strip(), data, style=style)


def equip_kb(user_id: int) -> InlineKeyboardMarkup:
    """Три слота в ряд (надетый артефакт открывает карточку, пустой слот открывает список),
    ниже «Все артефакты» и «Назад»."""
    worn = equipped(D.get_user(user_id))
    slots = []
    for n in range(SLOTS):
        if n < len(worn):
            slots.append(_artifact_btn(worn[n], "\u2800" if worn[n].get("emoji") else "",
                                       f"equip:item:{worn[n]['key']}", ButtonStyle.SUCCESS))
        else:
            slots.append(_btn("➕", "equip:list", style=ButtonStyle.PRIMARY))
    return InlineKeyboardMarkup(
        inline_keyboard=[
            slots,
            [_btn("Все артефакты", "equip:list")],
            _back_row("menu:pet"),
        ]
    )


def list_kb(user_id: int) -> InlineKeyboardMarkup:
    """Артефакты по два в ряд: свободные зелёные, надетые синие с галочкой."""
    worn_keys = {a["key"] for a in equipped(D.get_user(user_id))}
    buttons = []
    for a in ARTIFACTS:
        on = a["key"] in worn_keys
        label = f"{a['name']} ✅" if on else a["name"]
        buttons.append(_artifact_btn(a, label, f"equip:item:{a['key']}",
                                     ButtonStyle.PRIMARY if on else ButtonStyle.SUCCESS))
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    rows.append(_back_row("menu:equip"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def item_kb(user_id: int, key: str) -> InlineKeyboardMarkup:
    """Надеть (зелёная; красная, если слоты заняты) или снять (красная)."""
    a = BY_KEY.get(key, ARTIFACTS[0])
    worn = [x["key"] for x in equipped(D.get_user(user_id))]
    if a["key"] in worn:
        action = _btn("Снять", f"equip:take:{a['key']}", style=ButtonStyle.DANGER)
    else:
        free = len(worn) < SLOTS
        action = _btn("Надеть", f"equip:wear:{a['key']}",
                      style=ButtonStyle.SUCCESS if free else ButtonStyle.DANGER)
    return InlineKeyboardMarkup(inline_keyboard=[[action], _back_row("equip:list")])


# ---------- Отправка ----------

async def send_equip(bot, chat_id: int, user_id: int) -> None:
    await D.send_rich_card(
        bot, chat_id, "equip", lambda with_image: equip_html(user_id, with_image), equip_kb(user_id)
    )


async def send_list(bot, chat_id: int, user_id: int) -> None:
    await D.send_rich_card(
        bot, chat_id, "equip_list", lambda with_image: list_html(user_id, with_image), list_kb(user_id)
    )


async def send_item(bot, chat_id: int, user_id: int, key: str) -> None:
    await D.send_rich_card(
        bot, chat_id, "equip_item", lambda with_image: item_html(user_id, key, with_image),
        item_kb(user_id, key),
    )


async def send_preview(bot, chat_id: int, user_id: int, key: str) -> None:
    """Предпросмотр для /img: без кнопок под сообщением."""
    html = {
        "equip": lambda w: equip_html(user_id, w),
        "equip_list": lambda w: list_html(user_id, w),
        "equip_item": lambda w: item_html(user_id, ARTIFACTS[0]["key"], w),
    }[key]
    await D.send_rich_card(bot, chat_id, key, html)


async def _require_user(callback: CallbackQuery) -> dict | None:
    user = D.get_user(callback.from_user.id)
    if user is None:
        await callback.answer("Сначала выбери питомца: /start", show_alert=True)
    return user


async def _delete(callback: CallbackQuery) -> None:
    try:
        await callback.message.delete()
    except Exception as e:  # сообщение могло уже пропасть
        logging.warning("Не удалось удалить сообщение: %s", e)


# ---------- Хендлеры ----------

@router.callback_query(F.data == "menu:equip")
async def on_equip(callback: CallbackQuery):
    if await _require_user(callback) is None:
        return
    await callback.answer()
    await _delete(callback)
    await send_equip(callback.bot, callback.message.chat.id, callback.from_user.id)


@router.callback_query(F.data == "equip:list")
async def on_list(callback: CallbackQuery):
    if await _require_user(callback) is None:
        return
    await callback.answer()
    await _delete(callback)
    await send_list(callback.bot, callback.message.chat.id, callback.from_user.id)


@router.callback_query(F.data.regexp(r"^equip:item:\w+$"))
async def on_item(callback: CallbackQuery):
    if await _require_user(callback) is None:
        return
    key = callback.data.split(":")[2]
    if key not in BY_KEY:
        await callback.answer("Такого артефакта нет", show_alert=True)
        return
    await callback.answer()
    await _delete(callback)
    await send_item(callback.bot, callback.message.chat.id, callback.from_user.id, key)


@router.callback_query(F.data.regexp(r"^equip:wear:\w+$"))
async def on_wear(callback: CallbackQuery):
    user = await _require_user(callback)
    if user is None:
        return
    key = callback.data.split(":")[2]
    if key not in BY_KEY:
        await callback.answer("Такого артефакта нет", show_alert=True)
        return

    worn = user.setdefault("equipment", [])
    if key in worn:
        await callback.answer("Этот артефакт уже надет.", show_alert=True)
        return
    if len(worn) >= SLOTS:
        await callback.answer(
            f"Все {SLOTS} слота заняты. Сними один из артефактов.", show_alert=True
        )
        return

    worn.append(key)
    D.save_users()
    await callback.answer(f"Надето: {BY_KEY[key]['name']}")
    await _delete(callback)
    await send_equip(callback.bot, callback.message.chat.id, callback.from_user.id)


@router.callback_query(F.data.regexp(r"^equip:take:\w+$"))
async def on_take(callback: CallbackQuery):
    user = await _require_user(callback)
    if user is None:
        return
    key = callback.data.split(":")[2]
    worn = user.setdefault("equipment", [])
    if key not in worn:
        await callback.answer("Этот артефакт не надет.", show_alert=True)
        return

    worn.remove(key)
    D.save_users()
    await callback.answer(f"Снято: {BY_KEY[key]['name']}")
    await _delete(callback)
    await send_equip(callback.bot, callback.message.chat.id, callback.from_user.id)
