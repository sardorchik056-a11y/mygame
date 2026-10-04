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


def get_user(user_id: int) -> dict | None:
    return users.get(str(user_id))


def create_user(user_id: int, pet_key: str) -> None:
    users[str(user_id)] = {
        "pet": pet_key,
        "level": 1,
        "xp": 0,
        "wins": 0,
        "losses": 0,
    }
    save_users()


def xp_needed(level: int) -> int:
    """Сколько опыта нужно, чтобы выйти с этого уровня на следующий."""
    return 100 + (level - 1) * 50


def add_xp(user_id: int, amount: int) -> int:
    """Начислить опыт (пригодится для арены). Возвращает, на сколько вырос уровень."""
    user = get_user(user_id)
    if user is None:
        return 0
    user["xp"] += amount
    gained = 0
    while user["xp"] >= xp_needed(user["level"]):
        user["xp"] -= xp_needed(user["level"])
        user["level"] += 1
        gained += 1
    save_users()
    return gained

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
        "stats": {"hp": 6, "atk": 10, "def": 4, "spd": 7, "luck": 5},
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
        "stats": {"hp": 7, "atk": 6, "def": 6, "spd": 7, "luck": 6},
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
        "stats": {"hp": 10, "atk": 5, "def": 10, "spd": 3, "luck": 4},
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
        for key, val in pet["stats"].items()
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
        f"<p><b>{custom_emoji(LEVEL_EMOJI)} Уровень {level}</b><br>"
        f"<b>{xp_bar(xp, need)} {xp}/{need} XP</b></p>"
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
    )


def menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Мой питомец",
                    callback_data="menu:pet",
                    style=ButtonStyle.PRIMARY,
                )
            ]
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


MENU_TEXT = (
    "<b>Главное меню</b>\n\n"
    "<i>Здесь ты можешь посмотреть на своего питомца: его уровень, "
    "опыт и характеристики.</i>"
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
    "выбора питомцев или на карточку питомца. Галочка значит, что фото уже есть.</i>"
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
    await message.answer(MENU_TEXT, reply_markup=menu_kb())


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


@dp.callback_query(F.data == "menu:back")
async def on_menu_back(callback: CallbackQuery):
    await callback.answer()
    await callback.message.delete()
    await callback.message.answer(MENU_TEXT, reply_markup=menu_kb())


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
