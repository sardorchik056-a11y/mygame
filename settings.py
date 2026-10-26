"""Раздел «Настройки».

Что здесь есть:
  * Никнейм в боте. Игрок присылает имя сообщением: форматирование (жирный, курсив,
    подчёркнутый, зачёркнутый, моноширинный) и кастомные эмодзи сохраняются. Это имя
    показывается в игре вместо имени из Telegram (главное меню, профиль, исследователи).
    Другим модулям достаточно вызвать name_html(user) (для текста) или name_plain(user)
    (для кнопок и уведомлений).
  * Уведомления: «отряд вернулся из экспедиции». Фоновая задача notifier_loop шлёт
    сообщение, когда экспедиция закончилась. Список ожидающих уведомлений хранится в
    alerts.json рядом с базой, поэтому бот не перебирает всех игроков.
  * Информация об игроке: никнейм, ID, дата регистрации, уровень и бои.

Защита никнейма: длина, запрет ссылок и упоминаний, запрет переносов строк, пауза между
сменами, список запрещённых слов (BANNED_PARTS), проверка сохранённого HTML при каждом показе,
запись смен в audit.log. Админ может сбросить чужой никнейм из карточки игрока.

Данные игрока:
    user["nick"]        HTML никнейма (только безопасные теги)
    user["nick_plain"]  тот же никнейм обычным текстом
    user["nick_at"]     время последней смены
    user["tg_name"]     имя из Telegram (запасной вариант, обновляет панель админа)
    user["settings"]    {"notify_exrn": True/False}

Подключение в main.py: settings.setup(...), dp.include_router(settings.router) и запуск
settings.notifier_loop(bot) отдельной задачей. Старый обработчик «Настройки» в main.py нужно убрать.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from html import escape
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.enums import ButtonStyle
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.text_decorations import html_decoration

try:
    from storage import atomic_write_json, ledger as _ledger, read_json_safe
except ImportError:                      # запуск модуля отдельно (например, в тестах)
    _ledger = None
    atomic_write_json = read_json_safe = None

log = logging.getLogger(__name__)
router = Router()

# Куда админ может добавить фото
IMG_TARGETS: dict[str, str] = {
    "settings": "Настройки",
    "settings_nick": "Настройки: никнейм",
    "settings_notify": "Настройки: уведомления",
}

# ---------------------------------------------------------------------------
# Настройки
# ---------------------------------------------------------------------------

NICK_MIN = 2                     # минимум видимых символов
NICK_MAX = 24                    # максимум видимых символов
NICK_COOLDOWN = 300              # пауза между сменами никнейма, секунд
BANNED_PARTS: tuple[str, ...] = ()   # запрещённые слова (в нижнем регистре), например ("admin",)
RESERVED_BUTTONS = ("Меню", "Арена", "Рынок", "Настройки")   # кнопки нижней клавиатуры

# Какое форматирование сохраняем (остальное, например ссылки, отбрасывается)
ALLOWED_ENTITIES = {"bold", "italic", "underline", "strikethrough", "code", "custom_emoji"}
# Что запрещено в никнейме целиком
FORBIDDEN_ENTITIES = {"url", "text_link", "mention", "text_mention", "email", "phone_number",
                      "hashtag", "cashtag", "bot_command"}

ALERT_TICK = 20                  # как часто проверять уведомления, секунд
ALERTS_PER_TICK = 30             # сколько уведомлений отправлять за один проход
DEFAULT_SETTINGS = {"notify_exrn": True}

# Кастомный эмодзи заголовка (тот же, что на кнопке «Настройки»)
SETTINGS_EMOJI = ("5341715473882955310", "⚙️")

# ---------------------------------------------------------------------------
# Подключение к боту
# ---------------------------------------------------------------------------

_get_user = None
_save_users = None
_show_rich_card = None
_send_rich_card = None
_custom_emoji = None
_BACK_ID: str | None = None
_INFO = ("5334544901428229844", "ℹ️")
_ALERTS_FILE: Path | None = None
_ALERTS: dict[str, float] = {}   # uid -> когда закончилась (или закончится) экспедиция


def setup(*, get_user, save_users, show_rich_card, send_rich_card, custom_emoji,
          BACK_EMOJI_ID, INFO_EMOJI=None, alerts_path=None, **_ignored) -> None:
    global _get_user, _save_users, _show_rich_card, _send_rich_card, _custom_emoji
    global _BACK_ID, _INFO, _ALERTS_FILE
    _get_user, _save_users = get_user, save_users
    _show_rich_card, _send_rich_card = show_rich_card, send_rich_card
    _custom_emoji, _BACK_ID = custom_emoji, BACK_EMOJI_ID
    if INFO_EMOJI:
        _INFO = INFO_EMOJI
    if alerts_path:
        _ALERTS_FILE = Path(alerts_path)
        _load_alerts()


def _led(name: str, /, **fields) -> None:
    if _ledger is None:
        return
    try:
        _ledger(name, **fields)
    except Exception:
        log.exception("не удалось записать в audit.log: %s", name)


# ---------------------------------------------------------------------------
# Никнейм: безопасное имя для показа в других разделах
# ---------------------------------------------------------------------------

_TAG = re.compile(r'<(/?)(b|i|u|s|code|tg-emoji)((?:\s+emoji-id="\d{1,30}")?)>')


def _valid_html(value) -> bool:
    """Проверяет, что в сохранённом HTML только разрешённые теги, они закрыты и вложены верно."""
    if not isinstance(value, str) or not value or len(value) > 2000:
        return False
    stack: list[str] = []
    pos = 0
    for m in _TAG.finditer(value):
        if "<" in value[pos:m.start()] or ">" in value[pos:m.start()]:
            return False
        closing, tag, attrs = m.group(1), m.group(2), m.group(3)
        if closing:
            if attrs or not stack or stack.pop() != tag:
                return False
        else:
            if (tag == "tg-emoji") != bool(attrs):
                return False
            stack.append(tag)
        pos = m.end()
    tail = value[pos:]
    return not stack and "<" not in tail and ">" not in tail


def default_name(user: dict) -> str:
    """Имя без никнейма: имя из Telegram, иначе юзернейм, иначе «Игрок»."""
    return (user.get("tg_name") or user.get("username") or "Игрок")[:40]


def name_html(user: dict | None) -> str:
    """Имя игрока для вставки в HTML-сообщения (с форматированием и кастомными эмодзи)."""
    if not user:
        return "Игрок"
    nick = user.get("nick")
    if nick and _valid_html(nick):
        return nick
    return escape(default_name(user))


def name_plain(user: dict | None) -> str:
    """Имя игрока обычным текстом (для кнопок, подсказок, админки)."""
    if not user:
        return "Игрок"
    plain = user.get("nick_plain")
    if plain and isinstance(plain, str) and user.get("nick"):
        return plain[:60]
    return default_name(user)


def has_nick(user: dict) -> bool:
    return bool(user.get("nick")) and _valid_html(user.get("nick"))


def reset_nick(user: dict) -> str | None:
    """Убирает никнейм. Возвращает прежний (обычным текстом) или None."""
    old = user.get("nick_plain") if user.get("nick") else None
    for key in ("nick", "nick_plain", "nick_at"):
        user.pop(key, None)
    return old


def _nest_entities(entities: list) -> list:
    """Оставляет только вложенные или не пересекающиеся куски форматирования.
    Частично пересекающиеся (жирный и курсив внахлёст) ломают разметку, поэтому лишние отбрасываем."""
    kept: list = []
    stack: list[int] = []                     # концы открытых кусков
    for e in sorted(entities, key=lambda x: (x.offset, -x.length)):
        end = e.offset + e.length
        while stack and stack[-1] <= e.offset:
            stack.pop()
        if stack and end > stack[-1]:
            continue                          # пересекается частично: пропускаем
        kept.append(e)
        stack.append(end)
    return kept


def _check_text(message: Message) -> tuple[str | None, str | None, str | None]:
    """Разбирает сообщение с именем. Возвращает (html, обычный текст, ошибка)."""
    text = message.text or ""
    entities = list(message.entities or [])
    if not text.strip():
        return None, None, "Имя не должно быть пустым."
    if "\n" in text or "\r" in text:
        return None, None, "Имя должно быть в одну строку."
    if text.startswith("/"):
        return None, None, "Имя не может начинаться с «/»."
    if any(e.type in FORBIDDEN_ENTITIES for e in entities):
        return None, None, "В имени нельзя использовать ссылки, упоминания и хэштеги."
    if re.search(r"(https?://|t\.me/|www\.|@\w)", text, re.I):
        return None, None, "В имени нельзя использовать ссылки и упоминания."
    visible = len(text)
    if visible < NICK_MIN:
        return None, None, f"Слишком коротко: минимум {NICK_MIN} символа."
    if visible > NICK_MAX:
        return None, None, f"Слишком длинно: максимум {NICK_MAX} символов, у тебя {visible}."
    low = text.lower()
    if any(part in low for part in BANNED_PARTS):
        return None, None, "Это имя использовать нельзя. Придумай другое."
    if any(ord(ch) < 32 or ch in "\u200b\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"
           for ch in text):
        return None, None, "В имени есть недопустимые символы."
    kept = _nest_entities([e for e in entities if e.type in ALLOWED_ENTITIES])
    try:
        html = html_decoration.unparse(text, kept)
    except Exception:                          # битые смещения форматирования
        return None, None, "Не получилось разобрать форматирование. Попробуй другое имя."
    if not _valid_html(html):
        return None, None, "Не получилось разобрать форматирование. Попробуй другое имя."
    return html, text, None


# ---------------------------------------------------------------------------
# Уведомления об экспедициях
# ---------------------------------------------------------------------------

def get_setting(user: dict, key: str) -> bool:
    st = user.get("settings")
    if isinstance(st, dict) and key in st:
        return bool(st[key])
    return DEFAULT_SETTINGS.get(key, False)


def _load_alerts() -> None:
    if _ALERTS_FILE is None or read_json_safe is None:
        return
    try:
        saved = read_json_safe(_ALERTS_FILE)
    except RuntimeError:
        log.critical("alerts.json повреждён и копии нет: ожидающие уведомления начаты с нуля")
        return
    if isinstance(saved, dict):
        for uid, end in saved.items():
            try:
                _ALERTS[str(uid)] = float(end)
            except (TypeError, ValueError):
                continue


def _save_alerts() -> None:
    if _ALERTS_FILE is None or atomic_write_json is None:
        return
    try:
        atomic_write_json(_ALERTS_FILE, _ALERTS, backup_every=300)
    except OSError:
        log.exception("не удалось сохранить alerts.json")


def register_alert(uid, end: float) -> None:
    """Запоминает, что надо сообщить игроку, когда закончится экспедиция."""
    _ALERTS[str(uid)] = float(end)
    _save_alerts()


async def notifier_loop(bot: Bot) -> None:
    """Фоновая задача: шлёт «отряд вернулся», когда экспедиция закончилась."""
    while True:
        await asyncio.sleep(ALERT_TICK)
        try:
            await _notify_due(bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("ошибка в цикле уведомлений")


async def _notify_due(bot: Bot) -> None:
    now = time.time()
    due = [uid for uid, end in _ALERTS.items() if end <= now][:ALERTS_PER_TICK]
    if not due:
        return
    for uid in due:
        _ALERTS.pop(uid, None)
        user = _get_user(int(uid)) if uid.isdigit() else None
        ex = user.get("exrn") if user else None
        if not isinstance(ex, dict):
            continue
        trips = ex.get("trips") if isinstance(ex.get("trips"), dict) else {}
        for key, trip in list(trips.items()):
            if not isinstance(trip, dict) or trip.get("notified") or trip.get("end", 0) > now:
                continue
            if not get_setting(user, "notify_exrn"):
                continue
            await _send_return_alert(bot, int(uid), user, key, trip)
            trip["notified"] = True
            _save_users()
            await asyncio.sleep(0.05)
    _save_alerts()


async def _send_return_alert(bot: Bot, uid: int, user: dict, key: str, trip: dict) -> None:
    import exrn      # поздний импорт: exrn тоже использует settings

    g = exrn.BY_KEY.get(key)
    if g is None:
        return
    text = (
        f"<b>{_custom_emoji(exrn.EXPLORE_EMOJI)} Отряд вернулся!</b>\n\n"
        f"{name_html(user)}, {g['icon']} <b>{g['name']}</b> завершили экспедицию на "
        f"{trip.get('hours', '?')} ч. Добыча сложена у входа в лагерь, осталось её забрать.\n\n"
        "<i>Отключить такие сообщения можно в «Настройки» → «Уведомления».</i>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="К лагерю", callback_data="menu:exrn", style=ButtonStyle.SUCCESS,
                             icon_custom_emoji_id=exrn.ICON_SEND),
    ]])
    try:
        await bot.send_message(uid, text, reply_markup=kb)
    except TelegramForbiddenError:
        # Игрок заблокировал бота: больше не пытаемся
        user.setdefault("settings", {})["notify_exrn"] = False
        _save_users()
    except TelegramAPIError as e:
        log.warning("не удалось отправить уведомление игроку %s: %s", uid, e)


# ---------------------------------------------------------------------------
# Экраны
# ---------------------------------------------------------------------------

class NickStates(StatesGroup):
    waiting = State()


def _em(pair) -> str:
    return _custom_emoji(pair)


def _tree(rows: list[tuple[str, str]], title: str = "") -> str:
    lines = []
    for n, (label, value) in enumerate(rows):
        mark = "└" if n == len(rows) - 1 else "├"
        lines.append(f"{mark} {label}" + (f": <b>{value}</b>" if value != "" else ""))
    head = f"<b>{title}</b><br>" if title else ""
    return f"<p>{head}{'<br>'.join(lines)}</p>"


def _gap() -> str:
    return "<p>&nbsp;</p>"


def _image(with_image: bool) -> str:
    return '<img src="tg://photo?id=pet"/>' if with_image else ""


def _kb(*rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[list(r) for r in rows if r])


def _btn(text: str, data: str, style: ButtonStyle = ButtonStyle.PRIMARY) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data, style=style)


def _back(data: str, text: str = "Назад") -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data, style=ButtonStyle.PRIMARY,
                                icon_custom_emoji_id=_BACK_ID)


def _date(ts) -> str:
    try:
        return time.strftime("%d.%m.%Y", time.localtime(float(ts))) if float(ts) > 0 else "давно"
    except (TypeError, ValueError, OverflowError):
        return "давно"


def settings_html(user: dict, uid: int, with_image: bool = False, note: str = "") -> str:
    notify = get_setting(user, "notify_exrn")
    profile = [
        ("👤 Никнейм", name_html(user)),
        ("🆔 ID", f"<code>{uid}</code>"),
        ("📅 В игре с", _date(user.get("joined", 0))),
        ("⭐ Уровень питомца", str(user.get("level", 1))),
        ("⚔️ Бои", f"{user.get('wins', 0)} побед · {user.get('losses', 0)} поражений"),
    ]
    alerts = [("🗺 Возвращение экспедиции", "включено ✅" if notify else "выключено ❌")]
    return (
        f"{_image(with_image)}"
        f"<p><b>{_em(SETTINGS_EMOJI)} НАСТРОЙКИ</b></p>"
        "<blockquote><i>Здесь ты можешь придумать себе имя, настроить уведомления "
        "и посмотреть данные своего профиля.</i></blockquote>"
        + _tree(profile, "Профиль")
        + _gap()
        + _tree(alerts, "Уведомления")
        + (f"<p>{note}</p>" if note else "")
    )


def settings_kb() -> InlineKeyboardMarkup:
    return _kb(
        [_btn("✏️ Никнейм", "set:nick", ButtonStyle.SUCCESS),
         _btn("🔔 Уведомления", "set:notif", ButtonStyle.PRIMARY)],
        [_btn("Закрыть", "set:close", ButtonStyle.DANGER)],
    )


def nick_html_screen(user: dict, with_image: bool = False, note: str = "") -> str:
    left = max(0, int(user.get("nick_at", 0) + NICK_COOLDOWN - time.time()))
    rules = [
        ("Длина", f"от {NICK_MIN} до {NICK_MAX} символов"),
        ("Форматирование", "жирный, курсив, подчёркнутый, зачёркнутый, моноширинный"),
        ("Эмодзи", "обычные и кастомные"),
        ("Нельзя", "ссылки, упоминания, переносы строк"),
    ]
    return (
        f"{_image(with_image)}"
        "<p><b>✏️ НИКНЕЙМ</b></p>"
        "<blockquote><i>Это имя будет показано везде в боте: в меню, профиле и разделах. "
        "Выдели часть текста и сделай её жирной или вставь кастомный эмодзи, "
        "и всё сохранится как есть.</i></blockquote>"
        + _tree([("Сейчас", name_html(user))], "Твоё имя")
        + _gap()
        + _tree(rules, "Правила")
        + _p("<b>Отправь новое имя одним сообщением.</b>"
             + (f"<br><i>Менять имя можно раз в {NICK_COOLDOWN // 60} мин, подожди ещё {left} с.</i>"
                if left else ""))
        + (f"<p>{note}</p>" if note else "")
    )


def _p(text: str) -> str:
    return f"<p>{text}</p>"


def nick_kb(user: dict) -> InlineKeyboardMarkup:
    rows = []
    if has_nick(user):
        rows.append([_btn("Сбросить к имени Telegram", "set:nick_reset", ButtonStyle.DANGER)])
    rows.append([_back("set:main")])
    return _kb(*rows)


def notif_html(user: dict, with_image: bool = False) -> str:
    notify = get_setting(user, "notify_exrn")
    return (
        f"{_image(with_image)}"
        "<p><b>🔔 УВЕДОМЛЕНИЯ</b></p>"
        "<blockquote><i>Бот может сообщить, когда твой отряд вернётся из экспедиции, "
        "чтобы добыча не ждала в лагере.</i></blockquote>"
        + _tree([("🗺 Возвращение экспедиции", "включено ✅" if notify else "выключено ❌")], "Сейчас")
        + _p("<i>Сообщение приходит один раз, когда экспедиция завершилась.</i>")
    )


def notif_kb(user: dict) -> InlineKeyboardMarkup:
    notify = get_setting(user, "notify_exrn")
    return _kb(
        [_btn("Выключить: возвращение отряда" if notify else "Включить: возвращение отряда",
              "set:notif_toggle", ButtonStyle.DANGER if notify else ButtonStyle.SUCCESS)],
        [_back("set:main")],
    )


# ---------------------------------------------------------------------------
# Обработчики
# ---------------------------------------------------------------------------

def _touch(user: dict, tg) -> None:
    """Обновляет имя из Telegram (запасное имя, пока нет никнейма)."""
    name = (tg.full_name or "")[:64]
    if name and user.get("tg_name") != name:
        user["tg_name"] = name
        _save_users()


async def _need_user(callback: CallbackQuery):
    user = _get_user(callback.from_user.id)
    if user is None:
        await callback.answer("Сначала выбери питомца: /start", show_alert=True)
        return None
    _touch(user, callback.from_user)
    return user


async def _show(callback: CallbackQuery, key: str, html_fn, kb: InlineKeyboardMarkup,
                force_new: bool = False) -> None:
    await _show_rich_card(callback, key, html_fn, kb, force_new=force_new)


@router.message(F.text == "Настройки")
async def open_settings(message: Message, state: FSMContext):
    await state.clear()
    user = _get_user(message.from_user.id)
    if user is None:
        await message.answer("<i>Сначала выбери питомца: /start</i>")
        return
    _touch(user, message.from_user)
    await _send_rich_card(message.bot, message.chat.id, "settings",
                          lambda wi: settings_html(user, message.from_user.id, wi), settings_kb())


@router.callback_query(F.data == "set:main")
async def on_main(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    user = await _need_user(callback)
    if user is None:
        return
    await callback.answer()
    uid = callback.from_user.id
    await _show(callback, "settings", lambda wi: settings_html(user, uid, wi), settings_kb())


@router.callback_query(F.data == "set:close")
async def on_close(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer()
    try:
        await callback.message.delete()
    except TelegramAPIError:
        pass


@router.callback_query(F.data == "set:nick")
async def on_nick(callback: CallbackQuery, state: FSMContext):
    user = await _need_user(callback)
    if user is None:
        return
    await callback.answer()
    await state.set_state(NickStates.waiting)
    await _show(callback, "settings_nick", lambda wi: nick_html_screen(user, wi), nick_kb(user))


@router.callback_query(F.data == "set:nick_reset")
async def on_nick_reset(callback: CallbackQuery, state: FSMContext):
    user = await _need_user(callback)
    if user is None:
        return
    old = reset_nick(user)
    _save_users()
    if old:
        _led("nick_reset", uid=str(callback.from_user.id), old=old)
    await state.set_state(NickStates.waiting)
    await callback.answer("Никнейм сброшен: теперь показывается имя из Telegram")
    await _show(callback, "settings_nick", lambda wi: nick_html_screen(user, wi), nick_kb(user))


@router.message(NickStates.waiting, F.text)
async def nick_receive(message: Message, state: FSMContext):
    text = message.text or ""
    if text in RESERVED_BUTTONS or text.startswith("/"):
        await state.clear()          # игрок нажал другую кнопку или команду: выходим из ввода имени
        raise SkipHandler
    user = _get_user(message.from_user.id)
    if user is None:
        await state.clear()
        return
    _touch(user, message.from_user)

    left = int(user.get("nick_at", 0) + NICK_COOLDOWN - time.time())
    if left > 0:
        await message.answer(f"<i>Имя можно менять раз в {NICK_COOLDOWN // 60} мин. "
                             f"Подожди ещё {left} с.</i>")
        return
    html, plain, error = _check_text(message)
    if error:
        await message.answer(f"<i>{escape(error)} Отправь другое имя.</i>")
        return

    old = name_plain(user) if has_nick(user) else None
    user["nick"], user["nick_plain"], user["nick_at"] = html, plain, time.time()
    _save_users()
    _led("nick_change", uid=str(message.from_user.id), old=old, new=plain, html=html)
    await state.clear()
    try:
        await message.delete()       # убираем сообщение игрока, чтобы чат оставался чистым
    except TelegramAPIError:
        pass
    uid = message.from_user.id
    await _send_rich_card(
        message.bot, message.chat.id, "settings",
        lambda wi: settings_html(user, uid, wi, "<b>✅ Никнейм изменён!</b>"), settings_kb())


@router.message(NickStates.waiting, ~F.text)
async def nick_not_text(message: Message):
    await message.answer("<i>Пришли имя текстом.</i>")


@router.callback_query(F.data == "set:notif")
async def on_notif(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    user = await _need_user(callback)
    if user is None:
        return
    await callback.answer()
    await _show(callback, "settings_notify", lambda wi: notif_html(user, wi), notif_kb(user))


@router.callback_query(F.data == "set:notif_toggle")
async def on_notif_toggle(callback: CallbackQuery):
    user = await _need_user(callback)
    if user is None:
        return
    st = user.get("settings")
    if not isinstance(st, dict):
        st = user["settings"] = {}
    st["notify_exrn"] = not get_setting(user, "notify_exrn")
    _save_users()
    _led("settings_toggle", uid=str(callback.from_user.id), key="notify_exrn", value=st["notify_exrn"])
    await callback.answer("Уведомления включены" if st["notify_exrn"] else "Уведомления выключены")
    await _show(callback, "settings_notify", lambda wi: notif_html(user, wi), notif_kb(user))
