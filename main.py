import asyncio
import logging

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ButtonStyle
from aiogram.filters import CommandStart
from aiogram.types import KeyboardButton, Message, ReplyKeyboardMarkup

BOT_TOKEN = "8712603440:AAGc7SV7cAuHYVYZbVv0dSpxUKtmKlDehqM"

dp = Dispatcher()


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


@dp.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer("Добро пожаловать! Выбери раздел:", reply_markup=main_menu())


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
    bot = Bot(token=BOT_TOKEN)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
