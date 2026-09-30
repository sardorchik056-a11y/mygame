import asyncio
import logging

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ButtonStyle, ParseMode
from aiogram.filters import CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

BOT_TOKEN = "8712603440:AAGc7SV7cAuHYVYZbVv0dSpxUKtmKlDehqM"

dp = Dispatcher()

# user_id -> pet_key. Для постоянного хранения замени на SQLite.
user_pets: dict[int, str] = {}

PETS = {
    "flame": {
        "name": "Пиро",
        "element": "Огонь",
        "story": (
            "Пиро родился в жерле потухшего вулкана, где до сих пор тлеют угли. "
            "Он вспыльчив, но предан хозяину до последнего вздоха. "
            "Говорят, его пламя не гаснет даже под проливным дождём."
        ),
    },
    "aqua": {
        "name": "Аква",
        "element": "Вода",
        "story": (
            "Аква вышла из глубин подземного озера, куда не проникает солнечный свет. "
            "Она спокойна, рассудительна и умеет ждать нужного момента. "
            "Её чешуя мерцает, как звёзды в ночной воде."
        ),
    },
    "terra": {
        "name": "Терра",
        "element": "Земля",
        "story": (
            "Терра проснулась среди древних скал, которые старше любого города. "
            "Она неторопливая и выносливая, её очень трудно сдвинуть с места. "
            "Каждый её шаг оставляет на камне цветущий след."
        ),
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


def pet_card_kb(pet_key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Выбрать",
                    callback_data=f"pet:pick:{pet_key}",
                    style=ButtonStyle.SUCCESS,
                ),
                InlineKeyboardButton(
                    text="Назад",
                    callback_data="pet:back",
                    style=ButtonStyle.DANGER,
                ),
            ]
        ]
    )


# ---------- Тексты ----------

PETS_LIST_TEXT = "<b>Выбери своего питомца</b>\n\nНажми на питомца, чтобы узнать о нём больше."


def pet_card_text(pet_key: str) -> str:
    pet = PETS[pet_key]
    return (
        f"<b>{pet['name']}</b>\n"
        f"Стихия: <b>{pet['element']}</b>\n\n"
        f"<i>{pet['story']}</i>"
    )


# ---------- Хендлеры ----------

@dp.message(CommandStart())
async def cmd_start(message: Message):
    # Если питомец уже выбран, сразу меню
    if message.from_user.id in user_pets:
        await message.answer("С возвращением!", reply_markup=main_menu())
        return

    await message.answer(
        "<b>Добро пожаловать!</b>\n\n"
        "Тебя ждёт мир, где рядом с тобой будет верный питомец. "
        "Вместе вы сможете сражаться на арене, торговать на рынке "
        "и становиться сильнее.\n\n"
        "Но сначала нужно найти себе спутника.",
        reply_markup=continue_kb(),
    )


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

    await callback.message.edit_text(
        pet_card_text(pet_key),
        reply_markup=pet_card_kb(pet_key),
    )
    await callback.answer()


@dp.callback_query(F.data == "pet:back")
async def on_pet_back(callback: CallbackQuery):
    await callback.message.edit_text(PETS_LIST_TEXT, reply_markup=pets_list_kb())
    await callback.answer()


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

    # Фиксируем выбор, кнопки убираются
    await callback.message.edit_text(
        f"Ты выбрал: <b>{pet['name']}</b> (стихия: {pet['element']})"
    )
    await callback.answer()

    # 1) сначала пожелание удачи
    await callback.message.answer(
        f"{pet['name']} теперь твой спутник. Удачи в приключениях, "
        f"пусть ваш путь будет долгим и победным!"
    )

    # 2) только потом меню
    await callback.message.answer("Главное меню", reply_markup=main_menu())


@dp.message(F.text == "Меню")
async def open_menu(message: Message):
    await message.answer("Главное меню", reply_markup=main_menu())


@dp.message(F.text == "Арена")
async def open_arena(message: Message):
    await message.answer("Арена")


@dp.message(F.text == "Рынок")
async def open_market(message: Message):
    await message.answer("Рынок")


@dp.message(F.text == "Настройки")
async def open_settings(message: Message):
    await message.answer("Настройки")


async def main():
    logging.basicConfig(level=logging.INFO)
    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
