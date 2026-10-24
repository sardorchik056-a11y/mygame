import asyncio
import json
import logging
import random
from collections import OrderedDict
from html import escape
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ButtonStyle, ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    InputRichMessage,
    InputRichMessageMedia,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

import admin
import boss
import cases
import equipment
import exrn
import mine
from guard import Guard, on_error
from storage import ProcessLock, UserStore, atomic_write_json, read_json_safe

BOT_TOKEN = "8867617327:AAGP427kiEOg1aI8hRIUOyiH3Mu7eQGaEbE"

# Telegram ID админов (свой ID можно узнать у @userinfobot)
ADMIN_IDS: set[int] = {8118184388}

dp = Dispatcher()

# Фото питомцев: pet_key -> file_id. Хранится в файле рядом с ботом.
IMAGES_FILE = Path(__file__).with_name("pet_images.json")


def load_images() -> dict[str, str]:
    try:
        return read_json_safe(IMAGES_FILE)   # при поломке файла берёт копию .bak
    except RuntimeError:
        logging.getLogger(__name__).critical("pet_images.json повреждён и копии нет")
        return {}


def save_images() -> None:
    atomic_write_json(IMAGES_FILE, pet_images, backup_every=300)


pet_images: dict[str, str] = load_images()

# Данные игроков лежат в SQLite (users.db) рядом с ботом, подробности в storage.py:
# запись пачками раз в секунду, в памяти только недавно активные игроки, бэкапы каждый час.
# Старый users.json при первом запуске переносится в базу автоматически.
# "user_id": {"pet": key, "level": 1, "wins": 0, "losses": 0, ...}
USERS_FILE = Path(__file__).with_name("users.json")        # старый формат, нужен только для переноса
ProcessLock(Path(__file__).with_name("bot.lock")).acquire()  # вторая копия бота не запустится
store = UserStore(Path(__file__).with_name("users.db"), legacy_json=USERS_FILE)
users = store   # ведёт себя как словарь: users.get("123"), users["123"] = {...}


def save_users() -> None:
    """Отметить, что данные игроков менялись. Запись на диск пойдёт пачкой в течение секунды."""
    store.request_flush()


START_MEAT = 3
START_FRUIT = 4
START_COINS = 0


def get_user(user_id: int) -> dict | None:
    user = users.get(str(user_id))
    if user is not None:
        # Старые игроки без запасов тоже получают стартовый набор еды
        missing = False
        for field, default in (
            ("meat", START_MEAT),
            ("fruit", START_FRUIT),
            ("coins", START_COINS),
        ):
            if field not in user:
                user[field] = default
                missing = True
        if missing:
            save_users()
    return user


def create_user(user_id: int, pet_key: str) -> None:
    users[str(user_id)] = {
        "pet": pet_key,
        "level": 1,
        "wins": 0,
        "losses": 0,
        "meat": START_MEAT,
        "fruit": START_FRUIT,
        "coins": START_COINS,
    }
    save_users()


def add_coins(user_id: int, amount: int) -> bool:
    """Начислить монеты (amount > 0) или потратить (amount < 0).
    Возвращает False, если на списание не хватает монет."""
    user = get_user(user_id)
    if user is None or user["coins"] + amount < 0:
        return False
    user["coins"] += amount
    save_users()
    return True


COIN_EMOJI = ("5449418135381759397", "🪙")  # валюта бота: монеты
MEAT_EMOJI = "🥩"
INFO_EMOJI = ("5334544901428229844", "ℹ️")
FRUIT_EMOJI = "🥭"


MAX_LEVEL = 100
EVO_EVERY = 10      # эволюция при выходе на каждый 10-й уровень
EVO_COST_MULT = 7   # эволюция стоит в 7 раз больше еды
STAT_GROWTH = 0.04  # обычная прокачка: характеристики +4%
EVO_GROWTH = 0.15   # эволюция: характеристики +15%


def is_evolution(level: int) -> bool:
    """Прокачка с этого уровня на следующий — эволюция (10, 20, ... 100)."""
    return (level + 1) % EVO_EVERY == 0


def upgrade_cost(level: int) -> tuple[int, int]:
    """Сколько (мяса, фруктов) нужно, чтобы поднять уровень с текущего."""
    meat, fruit = level, level + 1
    if is_evolution(level):
        meat, fruit = meat * EVO_COST_MULT, fruit * EVO_COST_MULT
    return meat, fruit


def pet_stats(pet: dict, level: int) -> dict[str, int]:
    """Характеристики питомца на уровне: +4% за обычный уровень, +15% за эволюцию."""
    evolutions = level // EVO_EVERY
    regular = (level - 1) - evolutions
    mult = (1 + STAT_GROWTH) ** regular * (1 + EVO_GROWTH) ** evolutions
    return {key: round(val * mult) for key, val in pet["stats"].items()}


def upgrade_lack(user: dict) -> list[str]:
    """Чего не хватает для прокачки (пустой список, если всё есть).
    Нужны только мясо и фрукты."""
    level = user["level"]
    need_meat, need_fruit = upgrade_cost(level)
    lack = []
    if user["meat"] < need_meat:
        lack.append(f"мяса: {need_meat - user['meat']}")
    if user["fruit"] < need_fruit:
        lack.append(f"фруктов: {need_fruit - user['fruit']}")
    return lack


STAT_LABELS = {
    "hp": "Здоровье",
    "atk": "Атака",
    "def": "Защита",
    "spd": "Скорость",
    "luck": "Удача",
}

# Кастомные эмодзи: (id, запасной обычный эмодзи)
STAT_EMOJI = {
    "hp": ("5337080053119336309", "👍"),
    "atk": ("5321022334335724730", "🤺"),
    "def": ("5465154440287757794", "🛡"),
    "spd": ("5258203794772085854", "⚡️"),
    "luck": ("5422407403884798028", "🍀"),
}

# Эмодзи для подписей
ELEMENT_LABEL_EMOJI = ("5859548930458523065", "🔥")  # слово «Стихия»
SKILL_EMOJI = ("5364265456641258077", "⭐️")  # слово «Способность»

# Эмодзи профиля
LEVEL_EMOJI = ("5431816358675366190", "🆙")
WINS_EMOJI = ("5454014806950429357", "⚔️")
LOSSES_EMOJI = ("5285535716808342592", "☠️")
PET_BTN_EMOJI = ("5314714191314043019", "🐾")  # кнопка «Мой питомец»
MENU_EMOJI = ("5257965174979042426", "📋")  # заголовок меню
DONATE_EMOJI = ("5427168083074628963", "💎")  # кнопка «Задонатить»
CHECK_EMOJI = ("5206607081334906820", "✔️")  # «ресурса достаточно»
UPGRADE_EMOJI = ("5449683594425410231", "🔼")
EVO_EMOJI = ("5345857480213674463", "🌱")  # кнопка «Эволюционировать»  # кнопки «Прокачать»

# Эмодзи самой стихии
ELEMENT_EMOJI = {
    "Огонь": ("5424972470023104089", "🔥"),
    "Вода": ("5458654415307153329", "💧"),
    "Земля": ("6102899956383747910", "🌍"),
}

# Эмодзи кнопки «Назад»
BACK_EMOJI_ID = "6039539366177541657"


def custom_emoji(pair: tuple[str, str]) -> str:
    emoji_id, fallback = pair
    return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'


PETS = {
    "flame": {
        "name": "Пиро",
        "element": "Огонь",
        "rarity": "Эпический",
        "story": (
            "Пиро родился в жерле потухшего вулкана, где до сих пор тлеют угли. "
            "Он вспыльчив, но предан хозяину до последнего вздоха. "
            "Говорят, его пламя не гаснет даже под проливным дождём."
        ),
        "stats": {"hp": 60, "atk": 100, "def": 40, "spd": 70, "luck": 50},
        "skill": "Огненная ярость",
        "skill_desc": "Чем меньше здоровья, тем сильнее удары.",
    },
    "aqua": {
        "name": "Аква",
        "element": "Вода",
        "rarity": "Эпический",
        "story": (
            "Аква вышла из глубин подземного озера, куда не проникает солнечный свет. "
            "Она спокойна, рассудительна и умеет ждать нужного момента. "
            "Её чешуя мерцает, как звёзды в ночной воде."
        ),
        "stats": {"hp": 70, "atk": 60, "def": 60, "spd": 70, "luck": 60},
        "skill": "Приливная волна",
        "skill_desc": "Замедляет противника в начале боя.",
    },
    "terra": {
        "name": "Терра",
        "element": "Земля",
        "rarity": "Эпический",
        "story": (
            "Терра проснулась среди древних скал, которые старше любого города. "
            "Она неторопливая и выносливая, её очень трудно сдвинуть с места. "
            "Каждый её шаг оставляет на камне цветущий след."
        ),
        "stats": {"hp": 100, "atk": 50, "def": 100, "spd": 30, "luck": 40},
        "skill": "Каменная кожа",
        "skill_desc": "Поглощает часть входящего урона.",
    },
}


# ---------- Клавиатуры ----------

def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(
                    text="Меню",
                    style=ButtonStyle.PRIMARY,
                    icon_custom_emoji_id="5257965174979042426",
                ),
                KeyboardButton(
                    text="Арена",
                    style=ButtonStyle.DANGER,
                    icon_custom_emoji_id="5454014806950429357",
                ),
                KeyboardButton(
                    text="Рынок",
                    style=ButtonStyle.PRIMARY,
                    icon_custom_emoji_id="6010183144450299916",
                ),
            ],
            [
                KeyboardButton(
                    text="Настройки",
                    style=ButtonStyle.PRIMARY,
                    icon_custom_emoji_id="5341715473882955310",
                ),
            ],
        ],
        resize_keyboard=True,
    )


def continue_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Продолжить",
                    callback_data="intro:continue",
                    style=ButtonStyle.SUCCESS,
                )
            ]
        ]
    )


def pets_list_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"{pet['name']} — {pet['element']}",
                    callback_data=f"pet:view:{key}",
                    style=ButtonStyle.PRIMARY,
                )
            ]
            for key, pet in PETS.items()
        ]
    )


# ---------- Тексты ----------

WELCOME_TEXT = (
    "<b>Привет! Меня зовут Керри</b>\n\n"
    "<i>Я твой помощник и проводник по этому миру. "
    "Буду рядом, пока ты осваиваешься, и расскажу, что к чему.</i>\n\n"
    "<i>Здесь у каждого есть верный питомец. Вместе с ним ты будешь "
    "сражаться на арене, торговать на рынке и становиться сильнее.</i>\n\n"
    "<i>Но сначала тебе нужно найти спутника. "
    "Давай я покажу, кто уже ждёт своего хозяина.</i>"
)

PETS_LIST_TEXT = (
    "<b>Выбери своего питомца</b>\n\n"
    "<i>Вот кто ждёт тебя. Трое питомцев, и у каждого свой характер и своя сила. "
    "Нажми на любого, и я расскажу его историю и покажу, на что он способен.</i>\n\n"
    "<i>Выбирай сердцем, ведь питомца можно выбрать только один раз.</i>"
)


def pet_card_html(pet_key: str, with_image: bool = False, preview: bool = False) -> str:
    """Rich Message (Bot API 10.1+): весь текст жирным, история питомца курсивом."""
    pet = PETS[pet_key]

    rows = "".join(
        "<tr>"
        f"<td><b>{custom_emoji(STAT_EMOJI[key])} {escape(STAT_LABELS[key])}</b></td>"
        f'<td align="center"><b>{val}</b></td>'
        "</tr>"
        for key, val in pet["stats"].items()
    )

    element_emoji = custom_emoji(ELEMENT_EMOJI[pet["element"]])

    # Фото сверху (Bot API 10.2+: media передаётся отдельно, ссылка tg://photo?id=...)
    image = '<img src="tg://photo?id=pet"/>' if with_image else ""

    # Кнопка прямо в теле сообщения (Bot API 10.3); в предпросмотре админа её нет
    buttons = "" if preview else (
        "<tg-button-row>"
        f'<tg-button type="callback_data" data="pskills:sel:{pet_key}" '
        'style="primary">🌀 Навыки</tg-button>'
        "</tg-button-row>"
        "<tg-button-row>"
        f'<tg-button type="callback_data" data="pet:pick:{pet_key}" '
        'style="success">Выбрать</tg-button>'
        "</tg-button-row>"
    )

    return (
        f"{image}"
        # Имя и редкость, пустая строка, затем стихия (всё в одном абзаце через <br>)
        f"<p><b>{escape(pet['name'].upper())} · {escape(pet['rarity'])}</b>"
        "<br>&nbsp;<br>"
        f"<b>{custom_emoji(ELEMENT_LABEL_EMOJI)} Стихия: "
        f"{escape(pet['element'])} {element_emoji}</b></p>"
        # История питомца курсивом
        f"<blockquote><i>{escape(pet['story'])}</i></blockquote>"
        "<p><b>Характеристики</b></p>"
        "<table bordered striped>"
        "<tr><th><b>Параметр</b></th><th><b>Значение</b></th></tr>"
        f"{rows}"
        "</table>"
        f"<p><b>{custom_emoji(SKILL_EMOJI)} Способность: "
        f"{escape(pet['skill'])}</b></p>"
        f"<p><i>{escape(pet['skill_desc'])}</i></p>"
        f"{buttons}"
    )


def profile_html(user_id: int, with_image: bool = False) -> str:
    """Профиль питомца игрока: уровень, характеристики, бои, способность."""
    user = get_user(user_id)
    pet = PETS[user["pet"]]
    level = user["level"]

    base_stats = pet_stats(pet, level)
    stats_rows = "".join(
        "<tr>"
        f"<td><b>{custom_emoji(STAT_EMOJI[key])} {escape(STAT_LABELS[key])}</b></td>"
        # с артефактами: значение и сколько они добавили, например «112 (+12)»
        f'<td align="center"><b>{val}'
        f'{f" (+{val - base_stats[key]})" if val > base_stats[key] else ""}</b></td>'
        "</tr>"
        for key, val in equipment.user_stats(user).items()
    )

    battles_rows = "".join(
        "<tr>"
        f"<td><b>{label}</b></td>"
        f'<td align="center"><b>{value}</b></td>'
        "</tr>"
        for label, value in (
            (f"{custom_emoji(WINS_EMOJI)} Победы", user["wins"]),
            (f"{custom_emoji(LOSSES_EMOJI)} Поражения", user["losses"]),
        )
    )

    image = '<img src="tg://photo?id=pet"/>' if with_image else ""
    element_emoji = custom_emoji(ELEMENT_EMOJI[pet["element"]])

    return (
        f"{image}"
        f"<p><b>{escape(pet['name'].upper())} · {escape(pet['rarity'])}</b>"
        "<br>&nbsp;<br>"
        f"<b>{custom_emoji(ELEMENT_LABEL_EMOJI)} Стихия: "
        f"{escape(pet['element'])} {element_emoji}</b></p>"
        f"<p><b>{custom_emoji(LEVEL_EMOJI)} Уровень {level}/{MAX_LEVEL}</b>"
        + ("<br><b>Максимальный уровень</b></p>" if level >= MAX_LEVEL else "</p>")
        +
        "<p><b>Характеристики</b></p>"
        "<table bordered striped>"
        "<tr><th><b>Параметр</b></th><th><b>Значение</b></th></tr>"
        f"{stats_rows}"
        "</table>"
        "<p><b>Бои</b></p>"
        "<table bordered striped>"
        "<tr><th><b>Параметр</b></th><th><b>Значение</b></th></tr>"
        f"{battles_rows}"
        "</table>"
        f"<p><b>{custom_emoji(SKILL_EMOJI)} Способность: "
        f"{escape(pet['skill'])}</b></p>"
        f"<p><i>{escape(pet['skill_desc'])}</i></p>"
        # Кнопки прямо в теле сообщения (Bot API 10.3); «Прокачать» на макс. уровне нет
        + "<tg-button-row>"
        '<tg-button type="callback_data" data="pskills:prof" '
        'style="primary">🌀 Навыки</tg-button>'
        "</tg-button-row>"
        + (
            ""
            if level >= MAX_LEVEL
            else "<tg-button-row>"
            '<tg-button type="callback_data" data="pet:upgrade" '
            f'style="success">{custom_emoji(UPGRADE_EMOJI)} Прокачать уровень</tg-button>'
            "</tg-button-row>"
        )
    )


def stock_html(user_id: int | None = None, with_image: bool = False) -> str:
    """Rich-карточка запасов: не связана с питомцем, своё изображение (ключ «stock»)."""
    user = (get_user(user_id) if user_id else None) or {
        "coins": 0,
        "meat": 0,
        "fruit": 0,
    }

    rows = "".join(
        "<tr>"
        f"<td><b>{label}</b></td>"
        f'<td align="center"><b>{value}</b></td>'
        "</tr>"
        for label, value in (
            (f"{custom_emoji(COIN_EMOJI)} Монеты", user["coins"]),
            (f"{MEAT_EMOJI} Мясо", user["meat"]),
            (f"{FRUIT_EMOJI} Фрукты", user["fruit"]),
        )
    )

    image = '<img src="tg://photo?id=pet"/>' if with_image else ""

    return (
        f"{image}"
        f"<p><b>{custom_emoji(COIN_EMOJI)} ЗАПАСЫ</b></p>"
        "<blockquote><i>Всё, что ты собрал в пути. Монеты пригодятся на рынке, "
        "а еда нужна для роста питомца.</i></blockquote>"
        "<table bordered striped>"
        "<tr><th><b>Ресурс</b></th><th><b>Количество</b></th></tr>"
        f"{rows}"
        "</table>"
        f"<p><b>{custom_emoji(INFO_EMOJI)} Мясо и фрукты тратятся на прокачку "
        "и эволюцию питомца.</b></p>"
        # Синяя кнопка прямо в теле сообщения (Bot API 10.3)
        "<tg-button-row>"
        '<tg-button type="callback_data" data="stock:donate" '
        f'style="success">{custom_emoji(DONATE_EMOJI)} Задонатить</tg-button>'
        "</tg-button-row>"
    )


TREASURY_EMOJI = ("6192975148681793540", "💰")  # кнопка «Сокровищница»


def menu_kb() -> InlineKeyboardMarkup:
    def btn(text: str, data: str, style: ButtonStyle, emoji_id: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(
            text=text, callback_data=data, style=style, icon_custom_emoji_id=emoji_id
        )

    return InlineKeyboardMarkup(
        inline_keyboard=[
            # Главная кнопка на всю ширину, зелёная
            [btn("Мой питомец", "menu:pet", ButtonStyle.SUCCESS, PET_BTN_EMOJI[0])],
            [
                btn("Запасы", "menu:stock", ButtonStyle.PRIMARY, COIN_EMOJI[0]),
                btn("Шахты", "menu:mine", ButtonStyle.PRIMARY, mine.MINE_EMOJI[0]),
            ],
            [
                btn("Босс", "menu:boss", ButtonStyle.DANGER, boss.BOSS_EMOJI[0]),
                btn("Сокровищница", "menu:cases", ButtonStyle.PRIMARY, TREASURY_EMOJI[0]),
            ],
            [
                InlineKeyboardButton(
                    text="🧭 Исследователи",
                    callback_data="menu:exrn",
                    style=ButtonStyle.PRIMARY,
                )
            ],
        ]
    )


def profile_kb() -> InlineKeyboardMarkup:
    """Клавиатура профиля питомца: «Снаряжение» и «Назад»."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🎒 Снаряжение",
                    callback_data="menu:equip",
                    style=ButtonStyle.SUCCESS,
                )
            ],
            [
                InlineKeyboardButton(
                    text="Назад",
                    callback_data="menu:back",
                    style=ButtonStyle.PRIMARY,
                    icon_custom_emoji_id=BACK_EMOJI_ID,
                )
            ],
        ]
    )


def profile_back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Назад",
                    callback_data="menu:back",
                    style=ButtonStyle.PRIMARY,
                    icon_custom_emoji_id=BACK_EMOJI_ID,
                )
            ]
        ]
    )


def menu_html(user_id: int | None = None, with_image: bool = False) -> str:
    """Rich-карточка главного меню: своё изображение (ключ «menu») и короткий текст."""
    image = '<img src="tg://photo?id=pet"/>' if with_image else ""

    return (
        f"{image}"
        f"<p><b>{custom_emoji(MENU_EMOJI)} ГЛАВНОЕ МЕНЮ</b></p>"
        "<blockquote><i>Здесь начинается твой путь. Навести питомца, "
        "проверь запасы и отправляйся навстречу новым победам.</i></blockquote>"
    )


async def send_menu(bot: Bot, chat_id: int, user_id: int) -> None:
    await send_rich_card(
        bot,
        chat_id,
        "menu",
        lambda with_image: menu_html(user_id, with_image),
        menu_kb(),
    )


def back_kb() -> InlineKeyboardMarkup:
    # Обычная инлайн-кнопка под сообщением: синяя, с кастомным эмодзи
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Назад",
                    callback_data="pet:back",
                    style=ButtonStyle.PRIMARY,
                    icon_custom_emoji_id=BACK_EMOJI_ID,
                )
            ]
        ]
    )


# Что показано в сообщениях бота: (chat_id, message_id) -> (вид, file_id картинки).
# По этому решаем, можно ли обновить сообщение на месте (картинка та же) или нужно
# удалить его и отправить новое (картинка другая или сменился вид сообщения).
# После перезапуска бота память пуста: тогда безопасно удаляем и отправляем заново.
_MSG_STATE: "OrderedDict[tuple[int, int], tuple[str, str | None]]" = OrderedDict()
_MSG_STATE_MAX = 20000


def _remember(msg, kind: str, image: str | None) -> None:
    if msg is None or not hasattr(msg, "message_id"):
        return
    _MSG_STATE[(msg.chat.id, msg.message_id)] = (kind, image)
    _MSG_STATE.move_to_end((msg.chat.id, msg.message_id))
    while len(_MSG_STATE) > _MSG_STATE_MAX:
        _MSG_STATE.popitem(last=False)


def _rich_input(pet_key: str, build_html, use_image: bool = True):
    """Собирает rich-сообщение; возвращает (InputRichMessage, file_id или None)."""
    file_id = pet_images.get(pet_key) if use_image else None
    if file_id:
        return (
            InputRichMessage(
                html=build_html(True),
                media=[InputRichMessageMedia(id="pet", media=InputMediaPhoto(media=file_id))],
            ),
            file_id,
        )
    return InputRichMessage(html=build_html(False)), None


async def send_rich_card(
    bot: Bot,
    chat_id: int,
    pet_key: str,
    build_html,
    reply_markup: InlineKeyboardMarkup | None = None,
):
    """Отправляет rich-сообщение; если у питомца есть фото, ставит его сверху.
    Возвращает отправленное сообщение (нужно «живым» окнам босса)."""
    if pet_images.get(pet_key):
        try:
            rich, image = _rich_input(pet_key, build_html)
            msg = await bot.send_rich_message(
                chat_id=chat_id, rich_message=rich, reply_markup=reply_markup
            )
            _remember(msg, "rich", image)
            return msg
        except TelegramBadRequest as e:
            # Например, file_id устарел: показываем без фото
            logging.warning("Не удалось отправить фото %s: %s", pet_key, e)

    rich, _ = _rich_input(pet_key, build_html, use_image=False)
    msg = await bot.send_rich_message(
        chat_id=chat_id, rich_message=rich, reply_markup=reply_markup
    )
    _remember(msg, "rich", None)
    return msg


async def show_rich_card(
    callback: CallbackQuery,
    pet_key: str,
    build_html,
    reply_markup: InlineKeyboardMarkup | None = None,
    force_new: bool = False,
):
    """Показывает rich-экран вместо сообщения, на котором нажали кнопку.

    Картинка та же (или её нет и там и там): сообщение обновляется на месте.
    Картинка другая или сообщение другого вида: старое удаляется, отправляется новое.
    force_new=True: всегда удалить старое и отправить новое (переход из главного меню)."""
    msg = callback.message
    bot, chat_id = callback.bot, msg.chat.id
    cur = _MSG_STATE.get((chat_id, msg.message_id))
    if (not force_new and cur is not None and cur[0] == "rich"
            and cur[1] == pet_images.get(pet_key)):
        try:
            rich, image = _rich_input(pet_key, build_html)
            edited = await bot.edit_message_text(
                chat_id=chat_id,
                message_id=msg.message_id,
                rich_message=rich,
                reply_markup=reply_markup,
            )
            _remember(msg, "rich", image)
            return edited if not isinstance(edited, bool) else msg
        except TelegramBadRequest as e:
            if "not modified" in str(e):
                return msg
            logging.warning("Не удалось обновить сообщение на месте, шлю новое: %s", e)
    try:
        await msg.delete()
    except TelegramBadRequest:
        pass
    _MSG_STATE.pop((chat_id, msg.message_id), None)
    return await send_rich_card(bot, chat_id, pet_key, build_html, reply_markup)


async def send_pet_card(
    bot: Bot, chat_id: int, pet_key: str, preview: bool = False
) -> None:
    await send_rich_card(
        bot,
        chat_id,
        pet_key,
        lambda with_image: pet_card_html(pet_key, with_image, preview),
        None if preview else back_kb(),
    )


# Куда админ может добавить фото: сообщения-экраны и карточки питомцев
IMG_TARGETS: dict[str, str] = {
    "welcome": "Приветствие (Керри)",
    "pets_list": "Выбор питомцев",
    "menu": "Меню",
    "stock": "Запасы",
    **mine.IMG_TARGETS,
    **boss.IMG_TARGETS,
    **cases.IMG_TARGETS,
    **equipment.IMG_TARGETS,
    **exrn.IMG_TARGETS,
    **{key: pet["name"] for key, pet in PETS.items()},
}


async def send_screen(
    bot: Bot,
    chat_id: int,
    key: str,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
):
    """Обычное сообщение; если для экрана добавлено фото, текст идёт подписью к нему."""
    file_id = pet_images.get(key)

    if file_id:
        try:
            msg = await bot.send_photo(
                chat_id=chat_id, photo=file_id, caption=text, reply_markup=reply_markup
            )
            _remember(msg, "photo", file_id)
            return msg
        except TelegramBadRequest as e:
            # Например, file_id устарел или подпись слишком длинная (лимит 1024)
            logging.warning("Не удалось отправить фото экрана %s: %s", key, e)

    msg = await bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)
    _remember(msg, "text", None)
    return msg


async def show_screen(
    callback: CallbackQuery,
    key: str,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
):
    """Как show_rich_card, но для обычных экранов (текст или фото с подписью)."""
    msg = callback.message
    bot, chat_id = callback.bot, msg.chat.id
    target = pet_images.get(key)
    cur = _MSG_STATE.get((chat_id, msg.message_id))
    if cur is not None and cur[1] == target and cur[0] in ("text", "photo") \
            and (cur[0] == "photo") == bool(target):
        try:
            if target:
                await bot.edit_message_caption(
                    chat_id=chat_id, message_id=msg.message_id,
                    caption=text, reply_markup=reply_markup,
                )
            else:
                await bot.edit_message_text(
                    chat_id=chat_id, message_id=msg.message_id,
                    text=text, reply_markup=reply_markup,
                )
            return msg
        except TelegramBadRequest as e:
            if "not modified" in str(e):
                return msg
            logging.warning("Не удалось обновить экран на месте, шлю новый: %s", e)
    try:
        await msg.delete()
    except TelegramBadRequest:
        pass
    _MSG_STATE.pop((chat_id, msg.message_id), None)
    return await send_screen(bot, chat_id, key, text, reply_markup)


# ---------- Админка ----------
# Всё админское (панель /admin, /img, /give, статистика, игроки, рассылка, боссы,
# журнал, бан, технические работы) живёт в admin.py. Сюда передаём только нужные
# функции, данные и эмодзи.
admin.setup(
    ADMIN_IDS=ADMIN_IDS,
    users=users,
    store=store,
    get_user=get_user,
    save_users=save_users,
    pet_images=pet_images,
    save_images=save_images,
    IMG_TARGETS=IMG_TARGETS,
    PETS=PETS,
    MAX_LEVEL=MAX_LEVEL,
    custom_emoji=custom_emoji,
    COIN_EMOJI=COIN_EMOJI,
    FRUIT_EMOJI=FRUIT_EMOJI,
    MEAT_EMOJI=MEAT_EMOJI,
    BACK_EMOJI_ID=BACK_EMOJI_ID,
    send_pet_card=send_pet_card,
    send_screen=send_screen,
    send_rich_card=send_rich_card,
    menu_html=menu_html,
    stock_html=stock_html,
    WELCOME_TEXT=WELCOME_TEXT,
    PETS_LIST_TEXT=PETS_LIST_TEXT,
)
# Бан, технические работы и учёт игроков (юзернейм, последний визит): до Guard
admin.install(dp)

# Защита обработчиков (guard.py): антифлуд и очередь на игрока, чтобы частые нажатия
# не приводили к двойным тратам и двойным наградам; глобальный обработчик ошибок.
guard = Guard(admin_ids=ADMIN_IDS)
dp.message.outer_middleware(guard)
dp.callback_query.outer_middleware(guard)
dp.errors.register(on_error)

dp.include_router(admin.router)

# Раздел «Шахты» живёт в mine.py; ему передаём нужные функции и эмодзи
mine.setup(
    get_user=get_user,
    save_users=save_users,
    send_rich_card=send_rich_card,
    custom_emoji=custom_emoji,
    COIN_EMOJI=COIN_EMOJI,
    INFO_EMOJI=INFO_EMOJI,
    CHECK_EMOJI=CHECK_EMOJI,
    BACK_EMOJI_ID=BACK_EMOJI_ID,
)
dp.include_router(mine.router)

def drop_any_artifact(user: dict, tier: str):
    """Из сундука выпадает артефакт питомца, шахты или исследователей (поровну, в каждой редкости по 5 + 5 + 5)."""
    return random.choice((equipment.drop_artifact, mine.drop_artifact, exrn.drop_artifact))(user, tier)


def artifact_block(art: dict) -> str:
    """Карточка выпавшего артефакта: у артефактов шахты поле section == "mine"."""
    owner = {"mine": mine, "exrn": exrn}.get(art.get("section"), equipment)
    return owner.result_block(art)


# Раздел «Сокровищница» живёт в cases.py
cases.setup(
    artifacts=equipment.ARTIFACTS + mine.ARTIFACTS + exrn.ARTIFACTS,  # что может выпасть (все 75)
    drop_artifact=drop_any_artifact,           # выдать артефакт игроку
    result_block=artifact_block,               # карточка выпавшего артефакта
    get_user=get_user,
    save_users=save_users,
    send_rich_card=send_rich_card,
    custom_emoji=custom_emoji,
    COIN_EMOJI=COIN_EMOJI,
    INFO_EMOJI=INFO_EMOJI,
    BACK_EMOJI_ID=BACK_EMOJI_ID,
)
dp.include_router(cases.router)

# Раздел «Снаряжение» живёт в equipment.py
equipment.setup(
    get_user=get_user,
    save_users=save_users,
    send_rich_card=send_rich_card,
    custom_emoji=custom_emoji,
    INFO_EMOJI=INFO_EMOJI,
    BACK_EMOJI_ID=BACK_EMOJI_ID,
    PETS=PETS,
    pet_stats=pet_stats,
    STAT_EMOJI=STAT_EMOJI,
)
dp.include_router(equipment.router)

# Раздел «Босс» живёт в boss.py
boss.setup(
    user_stats=equipment.user_stats,  # характеристики с учётом артефактов
    bonus=equipment.bonus,            # бонусы артефактов: перезарядка, восстановление
    award_fragments=cases.award_fragments,  # фрагменты за урон по боссам
    get_user=get_user,
    save_users=save_users,
    send_rich_card=send_rich_card,
    custom_emoji=custom_emoji,
    COIN_EMOJI=COIN_EMOJI,
    INFO_EMOJI=INFO_EMOJI,
    BACK_EMOJI_ID=BACK_EMOJI_ID,
    PETS=PETS,
    pet_stats=pet_stats,
    pet_images=pet_images,
    STAT_EMOJI=STAT_EMOJI,
)
dp.include_router(boss.router)

# Раздел «Исследователи» живёт в exrn.py
exrn.setup(
    get_user=get_user,
    save_users=save_users,
    show_rich_card=show_rich_card,
    send_rich_card=send_rich_card,
    custom_emoji=custom_emoji,
    COIN_EMOJI=COIN_EMOJI,
    INFO_EMOJI=INFO_EMOJI,
    BACK_EMOJI_ID=BACK_EMOJI_ID,
    MEAT_EMOJI=MEAT_EMOJI,
    FRUIT_EMOJI=FRUIT_EMOJI,
)
dp.include_router(exrn.router)


# ---------- Хендлеры ----------

@dp.message(CommandStart())
async def cmd_start(message: Message):
    # Если питомец уже выбран, сразу меню
    if get_user(message.from_user.id):
        await message.answer("<b>С возвращением</b>", reply_markup=main_menu())
        return

    await send_screen(
        message.bot, message.chat.id, "welcome", WELCOME_TEXT, continue_kb()
    )


@dp.callback_query(F.data == "intro:continue")
async def on_continue(callback: CallbackQuery):
    await callback.answer()
    # Картинка та же: обновляем на месте, другая: удаляем и шлём новое
    await show_screen(callback, "pets_list", PETS_LIST_TEXT, pets_list_kb())


@dp.callback_query(F.data.startswith("pet:view:"))
async def on_pet_view(callback: CallbackQuery):
    pet_key = callback.data.split(":")[2]
    if pet_key not in PETS:
        await callback.answer("Неизвестный питомец", show_alert=True)
        return

    await callback.answer()
    await show_rich_card(
        callback, pet_key,
        lambda with_image: pet_card_html(pet_key, with_image, False),
        back_kb(),
    )


@dp.callback_query(F.data == "pet:back")
async def on_pet_back(callback: CallbackQuery):
    await callback.answer()
    await show_screen(callback, "pets_list", PETS_LIST_TEXT, pets_list_kb())


@dp.callback_query(F.data.startswith("pet:pick:"))
async def on_pet_pick(callback: CallbackQuery):
    user_id = callback.from_user.id

    if get_user(user_id):
        await callback.answer("Питомец уже выбран", show_alert=True)
        return

    pet_key = callback.data.split(":")[2]
    pet = PETS.get(pet_key)
    if pet is None:
        await callback.answer("Неизвестный питомец", show_alert=True)
        return

    create_user(user_id, pet_key)
    await callback.answer()

    # Карточка со своими кнопками убирается, вместо неё фиксируем выбор
    await callback.message.delete()
    await callback.message.answer(
        f"<b>Выбор сделан</b>\n\n"
        f"<i>Твой спутник:</i> <b>{pet['name']}</b>\n"
        f"<i>Стихия: {pet['element']}</i>"
    )

    # 1) сначала пожелание удачи
    await callback.message.answer(
        f"<i>{pet['name']} теперь рядом с тобой. "
        f"Удачи в приключениях, пусть ваш путь будет долгим и победным.</i>"
    )

    # 2) только потом меню
    await callback.message.answer("<b>Главное меню</b>", reply_markup=main_menu())


@dp.message(F.text == "Меню")
async def open_menu(message: Message):
    await send_menu(message.bot, message.chat.id, message.from_user.id)


@dp.callback_query(F.data == "menu:pet")
async def on_menu_pet(callback: CallbackQuery):
    user = get_user(callback.from_user.id)
    if user is None:
        await callback.answer("Сначала выбери питомца: /start", show_alert=True)
        return

    await callback.answer()
    # Из главного меню в раздел: всегда новое сообщение
    await show_rich_card(
        callback,
        user["pet"],
        lambda with_image: profile_html(callback.from_user.id, with_image),
        profile_kb(),
        force_new=True,
    )


@dp.callback_query(F.data == "menu:stock")
async def on_menu_stock(callback: CallbackQuery):
    user = get_user(callback.from_user.id)
    if user is None:
        await callback.answer("Сначала выбери питомца: /start", show_alert=True)
        return

    await callback.answer()
    await show_rich_card(
        callback,
        "stock",
        lambda with_image: stock_html(callback.from_user.id, with_image),
        profile_back_kb(),
        force_new=True,
    )


@dp.callback_query(F.data == "stock:donate")
async def on_stock_donate(callback: CallbackQuery):
    # Заглушка: донат пока не реализован
    await callback.answer("Донат скоро появится", show_alert=True)


@dp.callback_query(F.data == "menu:back")
async def on_menu_back(callback: CallbackQuery):
    await callback.answer()
    await show_rich_card(
        callback,
        "menu",
        lambda with_image: menu_html(callback.from_user.id, with_image),
        menu_kb(),
    )


def upgrade_html(user_id: int, with_image: bool = False) -> str:
    """Rich-карточка прокачки: уровень, таблица требований, кнопка в теле."""
    user = get_user(user_id)
    pet = PETS[user["pet"]]
    level = user["level"]

    image = '<img src="tg://photo?id=pet"/>' if with_image else ""
    element_emoji = custom_emoji(ELEMENT_EMOJI[pet["element"]])

    head = (
        f"{image}"
        f"<p><b>{escape(pet['name'].upper())} · {escape(pet['rarity'])}</b>"
        "<br>&nbsp;<br>"
        f"<b>{custom_emoji(ELEMENT_LABEL_EMOJI)} Стихия: "
        f"{escape(pet['element'])} {element_emoji}</b></p>"
    )

    if level >= MAX_LEVEL:
        return (
            head
            + f"<p><b>{custom_emoji(LEVEL_EMOJI)} Уровень {level}/{MAX_LEVEL}</b>"
            "<br>&nbsp;<br>"
            "<i>Питомец достиг максимального уровня. Дальше прокачивать некуда.</i></p>"
        )

    need_meat, need_fruit = upgrade_cost(level)
    evolution = is_evolution(level)

    needs = [
        (f"{MEAT_EMOJI} Мясо", user["meat"], need_meat),
        (f"{FRUIT_EMOJI} Фрукты", user["fruit"], need_fruit),
    ]

    rows = "".join(
        "<tr>"
        f"<td><b>{label}</b></td>"
        f'<td align="center"><b>{have}/{need}'
        f'{" " + custom_emoji(CHECK_EMOJI) if have >= need else ""}</b></td>'
        "</tr>"
        for label, have, need in needs
    )

    if evolution:
        kind = f"<i>Эволюция: характеристики +{round(EVO_GROWTH * 100)}%.</i>"
        title = f"Эволюция {level} → {level + 1}"
        button = "Эволюционировать"
        button_emoji = EVO_EMOJI
    else:
        kind = f"<i>Характеристики +{round(STAT_GROWTH * 100)}%.</i>"
        title = f"Уровень {level} → {level + 1}"
        button = "Прокачать"
        button_emoji = UPGRADE_EMOJI

    lack = upgrade_lack(user)
    status = (
        "<i>Всё готово, можно повышать уровень.</i>"
        if not lack
        else f"<i>Не хватает {escape(', '.join(lack))}.</i>"
    )

    return (
        head
        + f"<p><b>{custom_emoji(LEVEL_EMOJI)} {title}</b>"
        f"<br>{kind}"
        "<br>&nbsp;<br>"
        f"<b>{custom_emoji(INFO_EMOJI)} Для прокачки нужно</b></p>"
        "<table bordered striped>"
        "<tr><th><b>Ресурс</b></th><th><b>Есть/нужно</b></th></tr>"
        f"{rows}"
        "</table>"
        f"<p>{status}</p>"
        # Кнопка прямо в теле сообщения (Bot API 10.3)
        "<tg-button-row>"
        '<tg-button type="callback_data" data="upg:do" '
        f'style="success">{custom_emoji(button_emoji)} {button}</tg-button>'
        "</tg-button-row>"
    )


def upgrade_back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Назад",
                    callback_data="upg:back",
                    style=ButtonStyle.PRIMARY,
                    icon_custom_emoji_id=BACK_EMOJI_ID,
                )
            ]
        ]
    )


async def send_upgrade_card(bot: Bot, chat_id: int, user_id: int) -> None:
    user = get_user(user_id)
    await send_rich_card(
        bot,
        chat_id,
        user["pet"],
        lambda with_image: upgrade_html(user_id, with_image),
        upgrade_back_kb(),
    )


async def show_upgrade_card(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    user = get_user(user_id)
    await show_rich_card(
        callback,
        user["pet"],
        lambda with_image: upgrade_html(user_id, with_image),
        upgrade_back_kb(),
    )


@dp.callback_query(F.data == "pet:upgrade")
async def on_upgrade_open(callback: CallbackQuery):
    if get_user(callback.from_user.id) is None:
        await callback.answer("Сначала выбери питомца: /start", show_alert=True)
        return
    await callback.answer()
    await show_upgrade_card(callback)


@dp.callback_query(F.data == "upg:do")
async def on_upgrade_do(callback: CallbackQuery):
    user = get_user(callback.from_user.id)
    if user is None:
        await callback.answer("Сначала выбери питомца: /start", show_alert=True)
        return

    if user["level"] >= MAX_LEVEL:
        await callback.answer("Достигнут максимальный уровень", show_alert=True)
        return

    lack = upgrade_lack(user)
    if lack:
        await callback.answer("Не хватает " + ", ".join(lack), show_alert=True)
        return

    level = user["level"]
    evolution = is_evolution(level)
    need_meat, need_fruit = upgrade_cost(level)
    user["meat"] -= need_meat
    user["fruit"] -= need_fruit
    user["level"] += 1
    save_users()
    if evolution:
        await callback.answer(
            f"Эволюция! Уровень {user['level']}, "
            f"характеристики +{round(EVO_GROWTH * 100)}%",
            show_alert=True,
        )
    else:
        await callback.answer(f"Уровень повышен до {user['level']}!")

    # Обновляем карточку на месте, чтобы шкалы показали новый уровень
    await show_upgrade_card(callback)


@dp.callback_query(F.data == "upg:back")
async def on_upgrade_back(callback: CallbackQuery):
    user = get_user(callback.from_user.id)
    if user is None:
        await callback.answer("Сначала выбери питомца: /start", show_alert=True)
        return
    await callback.answer()
    await show_rich_card(
        callback,
        user["pet"],
        lambda with_image: profile_html(callback.from_user.id, with_image),
        profile_kb(),
    )


@dp.message(F.text == "Арена")
async def open_arena(message: Message):
    await message.answer("<b>Арена</b>")


@dp.message(F.text == "Рынок")
async def open_market(message: Message):
    await message.answer("<b>Рынок</b>")


@dp.message(F.text == "Настройки")
async def open_settings(message: Message):
    await message.answer("<b>Настройки</b>")


async def main():
    logging.basicConfig(level=logging.INFO)
    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    maintenance = asyncio.create_task(store.run_maintenance())   # пачечная запись, бэкапы, выгрузка из памяти
    logging.info("Игроков в базе: %d", len(store))
    try:
        await dp.start_polling(bot)
    finally:
        maintenance.cancel()
        store.close()   # финальная запись всего, что не успело уйти на диск


if __name__ == "__main__":
    asyncio.run(main())
