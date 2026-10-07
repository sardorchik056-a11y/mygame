import asyncio
import json
import logging
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

BOT_TOKEN = "8712603440:AAF7bO-ED3SB_sZV1w2T3ZEnkAZ52iWqSJ8"

# Telegram ID админов (свой ID можно узнать у @userinfobot)
ADMIN_IDS: set[int] = {8118184388}

dp = Dispatcher()

# Фото питомцев: pet_key -> file_id. Хранится в файле рядом с ботом.
IMAGES_FILE = Path(__file__).with_name("pet_images.json")


def load_images() -> dict[str, str]:
    try:
        return json.loads(IMAGES_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_images() -> None:
    IMAGES_FILE.write_text(
        json.dumps(pet_images, ensure_ascii=False, indent=2), encoding="utf-8"
    )


pet_images: dict[str, str] = load_images()

# Данные игроков хранятся в файле рядом с ботом.
# "user_id": {"pet": key, "level": 1, "xp": 0, "wins": 0, "losses": 0}
USERS_FILE = Path(__file__).with_name("users.json")


def load_users() -> dict[str, dict]:
    try:
        return json.loads(USERS_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_users() -> None:
    USERS_FILE.write_text(
        json.dumps(users, ensure_ascii=False, indent=2), encoding="utf-8"
    )


users: dict[str, dict] = load_users()


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
        "xp": 0,
        "wins": 0,
        "losses": 0,
        "meat": START_MEAT,
        "fruit": START_FRUIT,
        "coins": START_COINS,
    }
    save_users()


def xp_needed(level: int) -> int:
    """Сколько опыта нужно, чтобы выйти с этого уровня на следующий."""
    return 100 + (level - 1) * 50


def add_coins(user_id: int, amount: int) -> bool:
    """Начислить монеты (amount > 0) или потратить (amount < 0).
    Возвращает False, если на списание не хватает монет."""
    user = get_user(user_id)
    if user is None or user["coins"] + amount < 0:
        return False
    user["coins"] += amount
    save_users()
    return True


def add_xp(user_id: int, amount: int) -> None:
    """Начислить опыт (пригодится для арены). Уровень не повышается сам:
    его нужно прокачать за опыт, мясо и фрукты."""
    user = get_user(user_id)
    if user is None:
        return
    user["xp"] += amount
    save_users()


COIN_EMOJI = ("5449418135381759397", "🪙")  # валюта бота: монеты
MEAT_EMOJI = "🥩"
XP_EMOJI = ("5429578972771926029", "🟣")
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
    XP нужен всегда, в том числе для эволюции, в обычном размере."""
    level = user["level"]
    need_meat, need_fruit = upgrade_cost(level)
    need_xp = xp_needed(level)
    lack = []
    if user["xp"] < need_xp:
        lack.append(f"XP: {need_xp - user['xp']}")
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


def xp_bar(xp: int, need: int, width: int = 10) -> str:
    filled = min(width, int(width * xp / need))
    return "▰" * filled + "▱" * (width - filled)


def profile_html(user_id: int, with_image: bool = False) -> str:
    """Профиль питомца игрока: уровень, опыт, характеристики, бои, способность."""
    user = get_user(user_id)
    pet = PETS[user["pet"]]
    level, xp = user["level"], user["xp"]
    need = xp_needed(level)

    stats_rows = "".join(
        "<tr>"
        f"<td><b>{custom_emoji(STAT_EMOJI[key])} {escape(STAT_LABELS[key])}</b></td>"
        f'<td align="center"><b>{val}</b></td>'
        "</tr>"
        for key, val in pet_stats(pet, level).items()
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
        f"<p><b>{custom_emoji(LEVEL_EMOJI)} Уровень {level}/{MAX_LEVEL}</b><br>"
        + (
            "<b>Максимальный уровень</b></p>"
            if level >= MAX_LEVEL
            else f"<b>{xp_bar(xp, need)} {xp}/{need} XP</b></p>"
        )
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
        # Кнопка прямо в теле сообщения (Bot API 10.3); на макс. уровне её нет
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


def menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Мой питомец",
                    callback_data="menu:pet",
                    style=ButtonStyle.PRIMARY,
                    icon_custom_emoji_id=PET_BTN_EMOJI[0],
                )
            ],
            [
                InlineKeyboardButton(
                    text="Запасы",
                    callback_data="menu:stock",
                    style=ButtonStyle.PRIMARY,
                    icon_custom_emoji_id=COIN_EMOJI[0],
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
    """Rich-карточка главного меню: своё изображение (ключ «menu») и краткая сводка."""
    user = get_user(user_id) if user_id else None

    image = '<img src="tg://photo?id=pet"/>' if with_image else ""

    summary = ""
    if user is not None:
        pet = PETS[user["pet"]]
        element_emoji = custom_emoji(ELEMENT_EMOJI[pet["element"]])
        rows = "".join(
            "<tr>"
            f"<td><b>{label}</b></td>"
            f'<td align="center"><b>{value}</b></td>'
            "</tr>"
            for label, value in (
                (f"{custom_emoji(PET_BTN_EMOJI)} Питомец", escape(pet["name"])),
                (f"{custom_emoji(ELEMENT_LABEL_EMOJI)} Стихия",
                 f"{escape(pet['element'])} {element_emoji}"),
                (f"{custom_emoji(LEVEL_EMOJI)} Уровень", f"{user['level']}/{MAX_LEVEL}"),
                (f"{custom_emoji(COIN_EMOJI)} Монеты", user["coins"]),
            )
        )
        summary = (
            "<table bordered striped>"
            "<tr><th><b>Параметр</b></th><th><b>Значение</b></th></tr>"
            f"{rows}"
            "</table>"
        )

    return (
        f"{image}"
        f"<p><b>{custom_emoji(MENU_EMOJI)} ГЛАВНОЕ МЕНЮ</b></p>"
        "<blockquote><i>Отсюда начинается любое приключение. Загляни к питомцу, "
        "проверь запасы и готовься к новым боям.</i></blockquote>"
        f"{summary}"
        f"<p><b>{custom_emoji(INFO_EMOJI)} Выбери раздел ниже.</b></p>"
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


async def send_rich_card(
    bot: Bot,
    chat_id: int,
    pet_key: str,
    build_html,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    """Отправляет rich-сообщение; если у питомца есть фото, ставит его сверху."""
    file_id = pet_images.get(pet_key)

    if file_id:
        try:
            await bot.send_rich_message(
                chat_id=chat_id,
                rich_message=InputRichMessage(
                    html=build_html(True),
                    media=[
                        InputRichMessageMedia(
                            id="pet", media=InputMediaPhoto(media=file_id)
                        )
                    ],
                ),
                reply_markup=reply_markup,
            )
            return
        except TelegramBadRequest as e:
            # Например, file_id устарел: показываем без фото
            logging.warning("Не удалось отправить фото %s: %s", pet_key, e)

    await bot.send_rich_message(
        chat_id=chat_id,
        rich_message=InputRichMessage(html=build_html(False)),
        reply_markup=reply_markup,
    )


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
    **{key: pet["name"] for key, pet in PETS.items()},
}


async def send_screen(
    bot: Bot,
    chat_id: int,
    key: str,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    """Обычное сообщение; если для экрана добавлено фото, текст идёт подписью к нему."""
    file_id = pet_images.get(key)

    if file_id:
        try:
            await bot.send_photo(
                chat_id=chat_id, photo=file_id, caption=text, reply_markup=reply_markup
            )
            return
        except TelegramBadRequest as e:
            # Например, file_id устарел или подпись слишком длинная (лимит 1024)
            logging.warning("Не удалось отправить фото экрана %s: %s", key, e)

    await bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)


# ---------- Админка: /img ----------

class ImgStates(StatesGroup):
    waiting_photo = State()


admin_router = Router()
admin_router.message.filter(F.from_user.id.in_(ADMIN_IDS))
admin_router.callback_query.filter(F.from_user.id.in_(ADMIN_IDS))

IMG_MENU_TEXT = (
    "<b>Изображения питомцев</b>\n\n"
    "<i>Выбери, куда добавить или заменить фото: на приветствие, на экран "
    "выбора питомцев, на меню, на запасы или на карточку питомца. "
    "Галочка значит, что фото уже есть.</i>"
)


def img_pets_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"{'✅' if key in pet_images else '➕'} {title}",
                    callback_data=f"img:pet:{key}",
                    style=ButtonStyle.PRIMARY,
                )
            ]
            for key, title in IMG_TARGETS.items()
        ]
    )


def img_prompt_kb(pet_key: str) -> InlineKeyboardMarkup:
    row = [
        InlineKeyboardButton(
            text="Отмена", callback_data="img:cancel", style=ButtonStyle.DANGER
        )
    ]
    if pet_key in pet_images:
        row.insert(
            0,
            InlineKeyboardButton(
                text="Удалить фото",
                callback_data=f"img:del:{pet_key}",
                style=ButtonStyle.DANGER,
            ),
        )
    return InlineKeyboardMarkup(inline_keyboard=[row])


@admin_router.message(Command("img"))
async def cmd_img(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(IMG_MENU_TEXT, reply_markup=img_pets_kb())


@admin_router.callback_query(F.data.startswith("img:pet:"))
async def img_choose_pet(callback: CallbackQuery, state: FSMContext):
    pet_key = callback.data.split(":")[2]
    if pet_key not in IMG_TARGETS:
        await callback.answer("Неизвестная цель", show_alert=True)
        return

    await state.set_state(ImgStates.waiting_photo)
    await state.update_data(pet_key=pet_key)
    await callback.message.edit_text(
        f"<b>{IMG_TARGETS[pet_key]}</b>\n\n"
        "<i>Отправь фото одним сообщением (как фото, не файлом).</i>",
        reply_markup=img_prompt_kb(pet_key),
    )
    await callback.answer()


@admin_router.callback_query(F.data == "img:cancel")
async def img_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text(IMG_MENU_TEXT, reply_markup=img_pets_kb())
    await callback.answer()


@admin_router.callback_query(F.data.startswith("img:del:"))
async def img_delete(callback: CallbackQuery, state: FSMContext):
    pet_key = callback.data.split(":")[2]
    pet_images.pop(pet_key, None)
    save_images()
    await state.clear()
    await callback.message.edit_text(IMG_MENU_TEXT, reply_markup=img_pets_kb())
    await callback.answer("Фото удалено")


@admin_router.message(ImgStates.waiting_photo, F.photo)
async def img_receive(message: Message, state: FSMContext):
    data = await state.get_data()
    pet_key = data.get("pet_key")
    if pet_key not in IMG_TARGETS:
        await state.clear()
        return

    # Берём самое большое разрешение
    pet_images[pet_key] = message.photo[-1].file_id
    save_images()
    await state.clear()

    await message.answer(f"<b>Фото для «{IMG_TARGETS[pet_key]}» сохранено</b>")
    # Предпросмотр без кнопок
    if pet_key in PETS:
        await send_pet_card(message.bot, message.chat.id, pet_key, preview=True)
    elif pet_key == "welcome":
        await send_screen(message.bot, message.chat.id, "welcome", WELCOME_TEXT)
    elif pet_key == "menu":
        await send_rich_card(
            message.bot,
            message.chat.id,
            "menu",
            lambda with_image: menu_html(message.from_user.id, with_image),
        )
    elif pet_key == "stock":
        await send_rich_card(
            message.bot,
            message.chat.id,
            "stock",
            lambda with_image: stock_html(message.from_user.id, with_image),
        )
    else:
        await send_screen(message.bot, message.chat.id, "pets_list", PETS_LIST_TEXT)
    await message.answer(IMG_MENU_TEXT, reply_markup=img_pets_kb())


@admin_router.message(ImgStates.waiting_photo)
async def img_not_photo(message: Message):
    await message.answer("<i>Нужно отправить именно фото. Или нажми «Отмена».</i>")


dp.include_router(admin_router)


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
    # Текст -> фото с подписью: надёжнее удалить и отправить новое
    await callback.message.delete()
    await send_screen(
        callback.bot,
        callback.message.chat.id,
        "pets_list",
        PETS_LIST_TEXT,
        pets_list_kb(),
    )


@dp.callback_query(F.data.startswith("pet:view:"))
async def on_pet_view(callback: CallbackQuery):
    pet_key = callback.data.split(":")[2]
    if pet_key not in PETS:
        await callback.answer("Неизвестный питомец", show_alert=True)
        return

    await callback.answer()
    # Обычное сообщение -> rich: надёжнее удалить и отправить новое
    await callback.message.delete()
    await send_pet_card(callback.bot, callback.message.chat.id, pet_key)


@dp.callback_query(F.data == "pet:back")
async def on_pet_back(callback: CallbackQuery):
    await callback.answer()
    await callback.message.delete()
    await send_screen(
        callback.bot,
        callback.message.chat.id,
        "pets_list",
        PETS_LIST_TEXT,
        pets_list_kb(),
    )


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
    await callback.message.delete()
    await send_rich_card(
        callback.bot,
        callback.message.chat.id,
        user["pet"],
        lambda with_image: profile_html(callback.from_user.id, with_image),
        profile_back_kb(),
    )


@dp.callback_query(F.data == "menu:stock")
async def on_menu_stock(callback: CallbackQuery):
    user = get_user(callback.from_user.id)
    if user is None:
        await callback.answer("Сначала выбери питомца: /start", show_alert=True)
        return

    await callback.answer()
    await callback.message.delete()
    await send_rich_card(
        callback.bot,
        callback.message.chat.id,
        "stock",
        lambda with_image: stock_html(callback.from_user.id, with_image),
        profile_back_kb(),
    )


@dp.callback_query(F.data == "stock:donate")
async def on_stock_donate(callback: CallbackQuery):
    # Заглушка: донат пока не реализован
    await callback.answer("Донат скоро появится", show_alert=True)


@dp.callback_query(F.data == "menu:back")
async def on_menu_back(callback: CallbackQuery):
    await callback.answer()
    await callback.message.delete()
    await send_menu(
        callback.bot, callback.message.chat.id, callback.from_user.id
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
        (f"{custom_emoji(XP_EMOJI)} XP", user["xp"], xp_needed(level)),
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


@dp.callback_query(F.data == "pet:upgrade")
async def on_upgrade_open(callback: CallbackQuery):
    if get_user(callback.from_user.id) is None:
        await callback.answer("Сначала выбери питомца: /start", show_alert=True)
        return
    await callback.answer()
    # Старое сообщение удаляем, присылаем новое
    await callback.message.delete()
    await send_upgrade_card(
        callback.bot, callback.message.chat.id, callback.from_user.id
    )


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
    user["xp"] -= xp_needed(level)
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

    # Старое сообщение удаляем, присылаем новое с обновлёнными шкалами
    await callback.message.delete()
    await send_upgrade_card(
        callback.bot, callback.message.chat.id, callback.from_user.id
    )


@dp.callback_query(F.data == "upg:back")
async def on_upgrade_back(callback: CallbackQuery):
    user = get_user(callback.from_user.id)
    if user is None:
        await callback.answer("Сначала выбери питомца: /start", show_alert=True)
        return
    await callback.answer()
    await callback.message.delete()
    await send_rich_card(
        callback.bot,
        callback.message.chat.id,
        user["pet"],
        lambda with_image: profile_html(callback.from_user.id, with_image),
        profile_back_kb(),
    )


@admin_router.message(Command("give"))
async def cmd_give(message: Message):
    """/give <мясо> <фрукты> [XP] [монеты] — выдать себе ресурсы для теста."""
    user = get_user(message.from_user.id)
    if user is None:
        await message.answer("<i>Сначала выбери питомца: /start</i>")
        return
    parts = (message.text or "").split()[1:]
    try:
        meat, fruit = int(parts[0]), int(parts[1])
        xp = int(parts[2]) if len(parts) > 2 else 0
        coins = int(parts[3]) if len(parts) > 3 else 0
    except (IndexError, ValueError):
        await message.answer(
            "<i>Формат: /give 5 5 100 50 (мясо, фрукты, XP, монеты)</i>"
        )
        return
    user["meat"] += meat
    user["fruit"] += fruit
    user["xp"] += xp
    user["coins"] += coins
    save_users()
    await message.answer(
        "<b>Выдано</b>\n"
        f"{custom_emoji(COIN_EMOJI)} +{coins}  {MEAT_EMOJI} +{meat}  "
        f"{FRUIT_EMOJI} +{fruit}  {custom_emoji(XP_EMOJI)} +{xp}"
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
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
