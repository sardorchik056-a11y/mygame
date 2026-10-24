"""Защита обработчиков бота: порядок, антифлуд, ошибки.

Проблема. aiogram обрабатывает каждое обновление отдельной задачей, поэтому два нажатия
одного игрока (двойной тап, спам кнопкой) могут выполняться одновременно, перемежаясь на
каждом await. Классический результат: «проверил баланс, подождал, списал» дважды, то есть
двойная трата или двойная выдача награды.

Что делает Guard (регистрируется как внешний middleware на сообщения и колбэки):
  * Антифлуд: слишком частые нажатия одного игрока отбрасываются (колбэк при этом
    подтверждается, чтобы у кнопки не крутился индикатор).
  * Очередь на игрока: обработчики одного игрока выполняются строго по очереди, поэтому
    гонок между «проверкой» и «списанием» нет. Если очередь не дождалась за LOCK_TIMEOUT
    секунд, событие отбрасывается, а бот не зависает.
  * Блокировки не копятся: запись о блокировке удаляется, когда игрок свободен.

on_error: глобальный обработчик ошибок. Исключение в обработчике не оставляет игрока с
вечно «крутящейся» кнопкой: ошибка попадает в лог, игрок получает короткое сообщение.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, ErrorEvent

log = logging.getLogger("guard")

# Безобидные ошибки Telegram: не пугаем ими игрока
_BENIGN = (
    "message is not modified",
    "query is too old",
    "message to delete not found",
    "message can't be deleted",
    "message to edit not found",
)


async def _quiet_answer(event, text: str | None = None) -> None:
    try:
        await event.answer(text) if text else await event.answer()
    except Exception:
        pass


class Guard(BaseMiddleware):
    def __init__(self, admin_ids: set[int] | frozenset[int] = frozenset(),
                 callback_gap: float = 0.25, message_gap: float = 0.4,
                 lock_timeout: float = 15.0):
        self.admin_ids = set(admin_ids)
        self.callback_gap = callback_gap     # минимум между двумя нажатиями кнопок, секунд
        self.message_gap = message_gap       # минимум между двумя сообщениями, секунд
        self.lock_timeout = lock_timeout     # сколько ждать своей очереди, секунд
        self._last: dict[int, float] = {}
        self._locks: dict[int, list] = {}    # uid -> [Lock, число ожидающих и работающих]

    async def __call__(self, handler: Callable[[Any, dict], Awaitable[Any]],
                       event: Any, data: dict) -> Any:
        tg = getattr(event, "from_user", None)
        if tg is None:
            return await handler(event, data)
        uid = tg.id
        is_callback = isinstance(event, CallbackQuery)

        # 1. антифлуд
        now = time.monotonic()
        gap = self.callback_gap if is_callback else self.message_gap
        if uid not in self.admin_ids and now - self._last.get(uid, 0.0) < gap:
            if is_callback:
                await _quiet_answer(event)
            return None
        self._last[uid] = now
        if len(self._last) > 5000:  # чистим старые записи, чтобы словарь не рос
            cutoff = now - 60
            self._last = {k: v for k, v in self._last.items() if v >= cutoff}

        # 2. очередь на игрока
        entry = self._locks.get(uid)
        if entry is None:
            entry = self._locks[uid] = [asyncio.Lock(), 0]
        entry[1] += 1
        try:
            try:
                await asyncio.wait_for(entry[0].acquire(), self.lock_timeout)
            except asyncio.TimeoutError:
                log.warning("игрок %s: очередь обработчиков не освободилась за %.0f с", uid, self.lock_timeout)
                if is_callback:
                    await _quiet_answer(event, "Подожди секунду и попробуй ещё раз.")
                return None
            try:
                return await handler(event, data)
            finally:
                entry[0].release()
        finally:
            entry[1] -= 1
            if entry[1] == 0 and self._locks.get(uid) is entry:
                del self._locks[uid]


async def on_error(event: ErrorEvent) -> bool:
    """Глобальный обработчик ошибок: пишет в лог и вежливо отвечает игроку."""
    exc = event.exception
    update = event.update
    text = str(exc).lower()
    if isinstance(exc, TelegramAPIError) and any(b in text for b in _BENIGN):
        return True  # обычная мелочь (например, сообщение уже изменено)

    log.error("необработанная ошибка в обработчике: %r", exc, exc_info=exc)
    try:
        if update.callback_query is not None:
            await update.callback_query.answer("Что-то пошло не так. Попробуй ещё раз.")
        elif update.message is not None:
            await update.message.answer("<i>Что-то пошло не так. Попробуй ещё раз.</i>")
    except Exception:
        pass
    return True
