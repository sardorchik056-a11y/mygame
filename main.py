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

# user_id -> pet_key. Для постоянного хранения замени на SQLite.
user_pets: dict[int, str] = {}

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
    "<b>Добро пожаловать</b>\n\n"
    "<i>Тебя ждёт мир, где рядом с тобой будет верный питомец. "
    "Вместе вы сможете сражаться на арене, торговать на рынке "
    "и становиться сильнее.</i>\n\n"
    "<i>Но сначала нужно найти себе спутника.</i>"
)

PETS_LIST_TEXT = (
    "<b>Выбор питомца</b>\n\n"
    "<i>Три существа ждут своего хозяина. "
    "Нажми на любого, чтобы узнать его историю и силу.</i>"
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


async def send_pet_card(
    bot: Bot, chat_id: int, pet_key: str, preview: bool = False
) -> None:
    file_id = pet_images.get(pet_key)
    reply_markup = None if preview else back_kb()

    if file_id:
        try:
            await bot.send_rich_message(
                chat_id=chat_id,
                rich_message=InputRichMessage(
                    html=pet_card_html(pet_key, with_image=True, preview=preview),
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
            # Например, file_id устарел — показываем карточку без фото
            logging.warning("Не удалось отправить фото %s: %s", pet_key, e)

    await bot.send_rich_message(
        chat_id=chat_id,
        rich_message=InputRichMessage(html=pet_card_html(pet_key, preview=preview)),
        reply_markup=reply_markup,
    )


# ---------- Админка: /img ----------

class ImgStates(StatesGroup):
    waiting_photo = State()


admin_router = Router()
admin_router.message.filter(F.from_user.id.in_(ADMIN_IDS))
admin_router.callback_query.filter(F.from_user.id.in_(ADMIN_IDS))

IMG_MENU_TEXT = (
    "<b>Изображения питомцев</b>\n\n"
    "<i>Выбери питомца, чтобы добавить или заменить фото на его карточке. "
    "Галочка значит, что фото уже есть.</i>"
)


def img_pets_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"{'✅' if key in pet_images else '➕'} {pet['name']}",
                    callback_data=f"img:pet:{key}",
                    style=ButtonStyle.PRIMARY,
                )
            ]
            for key, pet in PETS.items()
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
    if pet_key not in PETS:
        await callback.answer("Неизвестный питомец", show_alert=True)
        return

    await state.set_state(ImgStates.waiting_photo)
    await state.update_data(pet_key=pet_key)
    await callback.message.edit_text(
        f"<b>{PETS[pet_key]['name']}</b>\n\n"
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
    if pet_key not in PETS:
        await state.clear()
        return

    # Берём самое большое разрешение
    pet_images[pet_key] = message.photo[-1].file_id
    save_images()
    await state.clear()

    await message.answer(f"<b>Фото для {PETS[pet_key]['name']} сохранено</b>")
    # Предпросмотр карточки (без кнопок выбора)
    await send_pet_card(message.bot, message.chat.id, pet_key, preview=True)
    await message.answer(IMG_MENU_TEXT, reply_markup=img_pets_kb())


@admin_router.message(ImgStates.waiting_photo)
async def img_not_photo(message: Message):
    await message.answer("<i>Нужно отправить именно фото. Или нажми «Отмена».</i>")


dp.include_router(admin_router)


# ---------- Хендлеры ----------

@dp.message(CommandStart())
async def cmd_start(message: Message):
    # Если питомец уже выбран, сразу меню
    if message.from_user.id in user_pets:
        await message.answer("<b>С возвращением</b>", reply_markup=main_menu())
        return

    await message.answer(WELCOME_TEXT, reply_markup=continue_kb())


@dp.callback_query(F.data == "intro:continue")
async def on_continue(callback: CallbackQuery):
    await callback.message.edit_text(PETS_LIST_TEXT, reply_markup=pets_list_kb())
    await callback.answer()


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
    await callback.message.answer(PETS_LIST_TEXT, reply_markup=pets_list_kb())


@dp.callback_query(F.data.startswith("pet:pick:"))
async def on_pet_pick(callback: CallbackQuery):
    user_id = callback.from_user.id

    if user_id in user_pets:
        await callback.answer("Питомец уже выбран", show_alert=True)
        return

    pet_key = callback.data.split(":")[2]
    pet = PETS.get(pet_key)
    if pet is None:
        await callback.answer("Неизвестный питомец", show_alert=True)
        return

    user_pets[user_id] = pet_key
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
    await message.answer("<b>Главное меню</b>", reply_markup=main_menu())


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
