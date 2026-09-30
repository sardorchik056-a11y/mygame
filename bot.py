import asyncio
import logging

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

BOT_TOKEN = "8712603440:AAGc7SV7cAuHYVYZbVv0dSpxUKtmKlDehqM"
DB_PATH = "bot.db"

# ID кастомных эмодзи
E_SUPPORT = '<tg-emoji emoji-id="5391112412445288650">🥸</tg-emoji>'
E_DENIED = '<tg-emoji emoji-id="5210952531676504517">❌</tg-emoji>'
E_MAIL = '<tg-emoji emoji-id="5253742260054409879">✉️</tg-emoji>'
E_WAIT = '<tg-emoji emoji-id="5386367538735104399">⌛</tg-emoji>'

TEXT_START = (
    f"{E_DENIED} <b>ДОСТУП ОГРАНИЧЕН</b>\n\n"
    "<i>Для начала работы сначала подайте заявку.</i>"
)
TEXT_SUBMITTED = (
    f"{E_MAIL} <b>ЗАЯВКА ПОДАНА, ОЖИДАЙТЕ РАССМОТРЕНИЯ</b>\n\n"
    f"{E_SUPPORT} <b>ПОДДЕРЖКА</b> @DqASAQ"
)
TEXT_ALREADY = f"<b>ЗАЯВКА УЖЕ В РАССМОТРЕНИИ</b> {E_WAIT}"

router = Router()


def apply_kb() -> InlineKeyboardMarkup:
    # style="success" -> зелёная кнопка (Bot API 9.4+, aiogram 3.25+)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Подать заявку",
                    callback_data="apply",
                    style="success",
                    icon_custom_emoji_id="5210952531676504517",
                )
            ]
        ]
    )


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "CREATE TABLE IF NOT EXISTS applications ("
            "user_id INTEGER PRIMARY KEY, "
            "username TEXT, "
            "created_at TEXT DEFAULT CURRENT_TIMESTAMP)"
        )
        await db.commit()


async def has_application(user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT 1 FROM applications WHERE user_id = ?", (user_id,)
        ) as cur:
            return await cur.fetchone() is not None


async def add_application(user_id: int, username: str | None) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO applications (user_id, username) VALUES (?, ?)",
            (user_id, username),
        )
        await db.commit()


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    if await has_application(message.from_user.id):
        await message.answer(TEXT_ALREADY)
        return
    await message.answer(TEXT_START, reply_markup=apply_kb())


@router.callback_query(F.data == "apply")
async def on_apply(call: CallbackQuery) -> None:
    user = call.from_user

    if await has_application(user.id):
        await call.answer()
        await call.message.answer(TEXT_ALREADY)
        return

    await add_application(user.id, user.username)
    await call.answer()
    await call.message.edit_text(TEXT_SUBMITTED, reply_markup=apply_kb())


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    await init_db()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
