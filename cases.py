"""Раздел «Кейсы»: кейсы, которые открываются за фрагменты.

Модуль самостоятельный, как mine.py и boss.py: main.py он не импортирует, нужные
функции передаются один раз через cases.setup(...).

Как это работает:
  * Фрагменты выпадают за урон по боссам: за каждые FRAG_DAMAGE_STEP урона даётся
    одна попытка, шанс получить 1 фрагмент в попытке FRAG_CHANCE. Остаток урона,
    который не добрал до попытки, копится дальше. Босс вызывает award_fragments(...)
    после каждого удара игрока.
  * Кейсов 10, у каждого своя цена во фрагментах (CASES).
  * Лут пока не добавлен: это оболочка. Пока у кейса пустой loot, открыть его нельзя,
    а фрагменты не списываются.
  * У каждого кейса своя карточка и своё фото (ключи case_1 ... case_10, задаются
    через /img).
"""

import logging
import random
from types import SimpleNamespace

from aiogram import F, Router
from aiogram.enums import ButtonStyle
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

router = Router()

# Заполняется в setup()
D = SimpleNamespace()


def setup(**deps) -> None:
    """Передаём модулю функции и эмодзи из main.py.

    Нужны: get_user, save_users, send_rich_card, custom_emoji, COIN_EMOJI,
    INFO_EMOJI, BACK_EMOJI_ID.
    """
    for name, value in deps.items():
        setattr(D, name, value)


# ---------- Баланс ----------

FRAG_DAMAGE_STEP = 5_000   # за каждые столько урона по боссу даётся попытка
FRAG_CHANCE = 0.5          # шанс получить фрагмент в одной попытке

FRAG = "🧩"                # значок фрагмента (можно заменить на кастомный эмодзи)

# Кейсы: цена во фрагментах, класс, значок и описание.
# loot: пока пустой, лут появится позже. Пока он пуст, открыть кейс нельзя.
CASES = [
    {"name": "Деревянный ящик", "price": 3, "tier": "Обычный", "icon": "🪵", "loot": [],
     "desc": "Скромный ящик, сколоченный из обломков. С чего-то же нужно начинать."},
    {"name": "Каменный сундук", "price": 6, "tier": "Обычный", "icon": "🪨", "loot": [],
     "desc": "Тяжёлый и обветренный, пахнет пещерой. Замок давно сломан, но крышка поддаётся туго."},
    {"name": "Бронзовый кейс", "price": 12, "tier": "Необычный", "icon": "🥉", "loot": [],
     "desc": "Потемневший от времени. Внутри что-то позвякивает."},
    {"name": "Железный кейс", "price": 20, "tier": "Необычный", "icon": "⚙️", "loot": [],
     "desc": "Клёпаный, с тремя замками. Кто-то явно не хотел, чтобы его открывали."},
    {"name": "Серебряный кейс", "price": 35, "tier": "Редкий", "icon": "🥈", "loot": [],
     "desc": "Холодный на ощупь и всегда начищенный. Выглядит дорого, и не зря."},
    {"name": "Золотой кейс", "price": 60, "tier": "Редкий", "icon": "🥇", "loot": [],
     "desc": "Сияет так, что видно из соседней пещеры. Открывается только по большим праздникам."},
    {"name": "Алмазный кейс", "price": 100, "tier": "Эпический", "icon": "💎", "loot": [],
     "desc": "Грани переливаются на свету. Говорят, внутри всегда лежит что-то особенное."},
    {"name": "Рунный кейс", "price": 160, "tier": "Эпический", "icon": "🔮", "loot": [],
     "desc": "Покрыт светящимися рунами. Они шепчут, но слов не разобрать."},
    {"name": "Космический кейс", "price": 260, "tier": "Легендарный", "icon": "🌌", "loot": [],
     "desc": "Упал со звёзд и до сих пор тёплый. Внутри слышен далёкий гул."},
    {"name": "Кейс Бездны", "price": 450, "tier": "Мифический", "icon": "🕳️", "loot": [],
     "desc": "Его достали из трещины между мирами. Лучше не слушать, что в нём скрипит."},
]

# Куда админ может добавить фото: список кейсов и карточка каждого кейса
IMG_TARGETS: dict[str, str] = {
    "cases": "Кейсы",
    **{f"case_{i + 1}": f"Кейс: {c['name']}" for i, c in enumerate(CASES)},
}


# ---------- Фрагменты ----------

def fragments_of(user: dict) -> int:
    return int(user.get("fragments", 0))


def award_fragments(user: dict, damage: int) -> int:
    """Начисляет фрагменты за урон по боссу. Возвращает, сколько выпало.

    Каждые FRAG_DAMAGE_STEP урона это одна попытка с шансом FRAG_CHANCE.
    Остаток урона запоминается и учитывается в следующих ударах."""
    pool = int(user.get("frag_dmg", 0)) + max(0, int(damage))
    attempts, user["frag_dmg"] = divmod(pool, FRAG_DAMAGE_STEP)
    gained = sum(1 for _ in range(attempts) if random.random() < FRAG_CHANCE)
    if gained:
        user["fragments"] = fragments_of(user) + gained
    return gained


def fmt(n: float) -> str:
    return f"{int(n):,}".replace(",", " ")


# ---------- Оформление ----------

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


def _bar(frac: float, size: int = 10) -> str:
    frac = min(1.0, max(0.0, frac))
    filled = round(frac * size)
    return "▰" * filled + "▱" * (size - filled) + f" {int(frac * 100)}%"


def _gap() -> str:
    """Пустая строка-отступ между блоками."""
    return "<p>&nbsp;</p>"


def _image(with_image: bool) -> str:
    return '<img src="tg://photo?id=pet"/>' if with_image else ""


def _coin() -> str:
    return D.custom_emoji(D.COIN_EMOJI)


def _info() -> str:
    return D.custom_emoji(D.INFO_EMOJI)


# ---------- Экраны ----------

def cases_html(user_id: int, with_image: bool = False) -> str:
    """Главное окно раздела: фрагменты, как их получить и все кейсы с ценами."""
    user = D.get_user(user_id) or {}
    have = fragments_of(user)
    progress = int(user.get("frag_dmg", 0))

    wallet = [
        (f"{FRAG} Фрагментов", fmt(have)),
        ("🎲 До следующей попытки", _bar(progress / FRAG_DAMAGE_STEP)),
    ]
    how = [
        ("🩸 Урон по боссам", f"каждые {fmt(FRAG_DAMAGE_STEP)} урона"),
        ("🎯 Попытка", f"{int(FRAG_CHANCE * 100)}% на 1 фрагмент"),
        ("💾 Остаток урона", "копится и не сгорает"),
    ]
    rows = [
        (f"{c['icon']} {c['name']}", f"{c['price']} {FRAG}", "✅" if have >= c["price"] else "🔒")
        for c in CASES
    ]
    return (
        f"{_image(with_image)}"
        f"<p><b>{FRAG} КЕЙСЫ</b></p>"
        "<blockquote><i>Бей боссов, собирай фрагменты и открывай кейсы. "
        "Чем крупнее кейс, тем ценнее добыча.</i></blockquote>"
        + _tree(wallet, "Твои фрагменты")
        + _gap()
        + _tree(how, "Как получить фрагменты")
        + _gap()
        + _table_n(("Кейс", "Цена", "Твой"), rows)
    )


def _case_index(n: int) -> int:
    return min(max(n, 0), len(CASES) - 1)


def case_html(user_id: int, n: int, with_image: bool = False) -> str:
    """Карточка кейса: что это, сколько стоит и хватает ли фрагментов."""
    n = _case_index(n)
    c = CASES[n]
    user = D.get_user(user_id) or {}
    have = fragments_of(user)
    missing = max(0, c["price"] - have)

    status = "можно открыть" if missing == 0 else f"не хватает {missing} {FRAG}"
    about = [
        (f"{c['icon']} Класс", c["tier"]),
        (f"{FRAG} Цена", fmt(c["price"])),
        ("🔢 Номер", f"{n + 1} из {len(CASES)}"),
    ]
    wallet = [
        (f"{FRAG} У тебя", fmt(have)),
        ("📊 Накоплено", _bar(have / c["price"])),
        ("🔓 Статус", status),
    ]
    loot = [("🔒 Лут", "появится позже")] if not c["loot"] else []
    return (
        f"{_image(with_image)}"
        f"<p><b>{c['icon']} {c['name'].upper()}</b><br>&nbsp;<br><b>{c['tier']} кейс</b></p>"
        f"<blockquote><i>{c['desc']}</i></blockquote>"
        + _tree(about, "О кейсе")
        + _gap()
        + _tree(wallet, "Твои фрагменты")
        + (_gap() + _tree(loot, "Содержимое") if loot else "")
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


def cases_kb(user_id: int) -> InlineKeyboardMarkup:
    """Десять кейсов двумя рядами по пять, только значки. Зелёная кнопка: хватает фрагментов,
    красная: не хватает."""
    have = fragments_of(D.get_user(user_id) or {})
    buttons = [
        _btn(c["icon"], f"cases:view:{i}",
             style=ButtonStyle.SUCCESS if have >= c["price"] else ButtonStyle.DANGER)
        for i, c in enumerate(CASES)
    ]
    return InlineKeyboardMarkup(
        inline_keyboard=[buttons[:5], buttons[5:], _back_row("menu:back")]
    )


def case_kb(user_id: int, n: int) -> InlineKeyboardMarkup:
    """Открыть кейс, листать соседние кейсы и вернуться к списку."""
    n = _case_index(n)
    c = CASES[n]
    have = fragments_of(D.get_user(user_id) or {})
    ready = have >= c["price"]
    open_btn = _btn(
        f"Открыть за {c['price']} {FRAG}",
        f"cases:open:{n}",
        style=ButtonStyle.SUCCESS if ready else ButtonStyle.DANGER,
    )
    prev_n, next_n = (n - 1) % len(CASES), (n + 1) % len(CASES)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [open_btn],
            [
                _btn("◀", f"cases:view:{prev_n}", style=ButtonStyle.PRIMARY),
                _btn("▶", f"cases:view:{next_n}", style=ButtonStyle.PRIMARY),
            ],
            _back_row("menu:cases"),
        ]
    )


# ---------- Отправка ----------

def _key_index(key: str) -> int:
    """case_3 -> 2 (индекс кейса в списке)."""
    return _case_index(int(key.split("_")[1]) - 1)


async def send_cases(bot, chat_id: int, user_id: int) -> None:
    await D.send_rich_card(
        bot, chat_id, "cases", lambda with_image: cases_html(user_id, with_image), cases_kb(user_id)
    )


async def send_case(bot, chat_id: int, user_id: int, n: int) -> None:
    n = _case_index(n)
    await D.send_rich_card(
        bot, chat_id, f"case_{n + 1}", lambda with_image: case_html(user_id, n, with_image),
        case_kb(user_id, n),
    )


async def send_preview(bot, chat_id: int, user_id: int, key: str) -> None:
    """Предпросмотр для /img: без кнопок под сообщением."""
    if key == "cases":
        await D.send_rich_card(
            bot, chat_id, "cases", lambda with_image: cases_html(user_id, with_image)
        )
    else:
        n = _key_index(key)
        await D.send_rich_card(
            bot, chat_id, key, lambda with_image: case_html(user_id, n, with_image)
        )


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

@router.callback_query(F.data == "menu:cases")
async def on_open(callback: CallbackQuery):
    if await _require_user(callback) is None:
        return
    await callback.answer()
    await _delete(callback)
    await send_cases(callback.bot, callback.message.chat.id, callback.from_user.id)


@router.callback_query(F.data.regexp(r"^cases:view:\d+$"))
async def on_view(callback: CallbackQuery):
    if await _require_user(callback) is None:
        return
    await callback.answer()
    n = _case_index(int(callback.data.split(":")[2]))
    await _delete(callback)
    await send_case(callback.bot, callback.message.chat.id, callback.from_user.id, n)


@router.callback_query(F.data.regexp(r"^cases:open:\d+$"))
async def on_open_case(callback: CallbackQuery):
    user = await _require_user(callback)
    if user is None:
        return
    n = _case_index(int(callback.data.split(":")[2]))
    c = CASES[n]
    have = fragments_of(user)

    if have < c["price"]:
        await callback.answer(
            f"Не хватает фрагментов: нужно {c['price']}, у тебя {have}.", show_alert=True
        )
        return
    if not c["loot"]:
        await callback.answer(
            f"{c['name']}: содержимое появится позже. Фрагменты не потрачены.",
            show_alert=True,
        )
        return
    # Когда в кейсах появится лут, открытие будет списывать c["price"] фрагментов здесь.
    await callback.answer()
