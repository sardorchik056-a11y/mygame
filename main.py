import asyncio
import logging
from html import escape

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ButtonStyle, ParseMode
from aiogram.filters import CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputRichMessage,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

BOT_TOKEN = "8712603440:AAF7bO-ED3SB_sZV1w2T3ZEnkAZ52iWqSJ8"

dp = Dispatcher()

# user_id -> pet_key. Для постоянного хранения замени на SQLite.
user_pets: dict[int, str] = {}

STAT_LABELS = {
    "hp": "Здоровье",
    "atk": "Атака",
    "def": "Защита",
    "spd": "Скорость",
    "luck": "Удача",
}

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


def pet_card_html(pet_key: str) -> str:
    """Rich Message (Bot API 10.1+): таблица характеристик и кнопки внутри сообщения."""
    pet = PETS[pet_key]

    rows = "".join(
        "<tr>"
        f"<td>{escape(STAT_LABELS[key])}</td>"
        f'<td align="center">{val}/10</td>'
        "</tr>"
        for key, val in pet["stats"].items()
    )

    return (
        f"<h2>{escape(pet['name'].upper())}</h2>"
        f"<p><i>Стихия: {escape(pet['element'])} · "
        f"Редкость: {escape(pet['rarity'])}</i></p>"
        f"<blockquote>{escape(pet['story'])}</blockquote>"
        "<h3>Характеристики</h3>"
        "<table bordered striped>"
        "<tr><th>Параметр</th><th>Значение</th></tr>"
        f"{rows}"
        "</table>"
        f"<p><b>Способность: {escape(pet['skill'])}</b></p>"
        f"<p><i>{escape(pet['skill_desc'])}</i></p>"
        # Кнопки прямо в теле сообщения (Bot API 10.3)
        "<tg-button-row>"
        f'<tg-button type="callback_data" data="pet:pick:{pet_key}" '
        'style="success">Выбрать</tg-button>'
        '<tg-button type="callback_data" data="pet:back" '
        'style="danger">Назад</tg-button>'
        "</tg-button-row>"
    )


async def send_pet_card(bot: Bot, chat_id: int, pet_key: str) -> None:
    await bot.send_rich_message(
        chat_id=chat_id,
        rich_message=InputRichMessage(html=pet_card_html(pet_key)),
    )


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
