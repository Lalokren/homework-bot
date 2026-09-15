"""
Telegram-бот для сбора домашних заданий в классном чате.

Что делает:
  • Слушает групповой чат и сохраняет все сообщения в SQLite.
  • По команде «бот дз» анализирует сообщения за день через нейросеть
    GigaChat и выводит список домашних заданий по предметам.
  • Понимает русские даты: «бот дз вчера», «бот дз 12 сентября».

Запуск:  python bot.py
"""

import asyncio
import html
import logging
import os
import re
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ChatMemberStatus
from aiogram.types import ChatMemberUpdated, Message
from dotenv import load_dotenv

from database import init_db, save_message, get_messages_by_date
from ai_service import process_messages, test_connection

# ---------------------------------------------------------------------------
# Настройка логирования
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Настройки из переменных окружения (.env)
# ---------------------------------------------------------------------------
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
GIGACHAT_API_KEY = os.getenv("GIGACHAT_API_KEY")
ALLOWED_CHAT_IDS = os.getenv("ALLOWED_CHAT_IDS", "")

if ALLOWED_CHAT_IDS:
    ALLOWED_CHAT_IDS = {int(x.strip()) for x in ALLOWED_CHAT_IDS.split(",") if x.strip()}
else:
    ALLOWED_CHAT_IDS = set()

if not BOT_TOKEN:
    raise SystemExit("ОШИБКА: не указан BOT_TOKEN в файле .env!")
if not GIGACHAT_API_KEY:
    raise SystemExit("ОШИБКА: не указан GIGACHAT_API_KEY в файле .env!")

# ---------------------------------------------------------------------------
# Инициализация бота
# ---------------------------------------------------------------------------
bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher()
router = Router()

MAX_REPLY_LENGTH = 4000  # Telegram не отправит длиннее

# ---------------------------------------------------------------------------
# Русские месяцы для разбора дат
# ---------------------------------------------------------------------------
RUSSIAN_MONTHS = {
    "январь": 1, "января": 1,
    "февраль": 2, "февраля": 2,
    "март": 3, "марта": 3,
    "апрель": 4, "апреля": 4,
    "май": 5, "мая": 5,
    "июнь": 6, "июня": 6,
    "июль": 7, "июля": 7,
    "август": 8, "августа": 8,
    "сентябрь": 9, "сентября": 9,
    "октябрь": 10, "октября": 10,
    "ноябрь": 11, "ноября": 11,
    "декабрь": 12, "декабря": 12,
}

MONTHS_NOMINATIVE = {
    1: "января", 2: "февраля", 3: "марта", 4: "апреля",
    5: "мая", 6: "июня", 7: "июля", 8: "августа",
    9: "сентября", 10: "октября", 11: "ноября", 12: "декабря",
}


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------

def parse_russian_date(text: str) -> datetime | None:
    """
    Разбирает русскую дату из текста.
    Понимает: «сегодня», «вчера», «позавчера», «12 сентября», «5 мая»,
    «12.09», «12.09.2026».
    """
    text = text.lower().strip()
    today = datetime.now()

    if not text or text == "сегодня":
        return today
    if text == "вчера":
        return today - timedelta(days=1)
    if text == "позавчера":
        return today - timedelta(days=2)

    # «12 сентября»
    match = re.fullmatch(r"(\d{1,2})\s+([а-яё]+)", text)
    if match:
        day = int(match.group(1))
        month = RUSSIAN_MONTHS.get(match.group(2))
        if month and 1 <= day <= 31:
            try:
                return datetime(today.year, month, day)
            except ValueError:
                return None

    # «12.09» / «12.09.2026»
    for fmt in ("%d.%m.%Y", "%d.%m"):
        try:
            dt = datetime.strptime(text, fmt)
            if dt.year <= 1900:
                dt = dt.replace(year=today.year)
            return dt
        except ValueError:
            continue

    return None


def get_message_link(chat_id: int, message_id: int) -> str:
    """https://t.me/c/{chat_id}/{message_id} для супергрупп."""
    str_id = str(abs(chat_id))
    if str_id.startswith("100"):
        str_id = str_id[3:]
    return f"https://t.me/c/{str_id}/{message_id}"


def format_homework(homework: list, date_display: str) -> str:
    """Красиво оформляет Д/З столбиком."""
    if not homework:
        return (
            f"📚 Д/З на {date_display}\n\n"
            "Ничего не нашёл. Похоже, сегодня ничего не задали 😌"
        )
    lines = [f"📚 Д/З на {date_display}", ""]
    for item in homework:
        subject = item.get("subject", "Неизвестно")
        text = item.get("text", "")
        link = item.get("link", "")
        link_part = f' <a href="{link}">📎</a>' if link else ""
        lines.append(f"▫️ {subject}: {text}{link_part}")
    return "\n".join(lines)


def check_chat(message: Message) -> bool:
    """Проверяет, разрешён ли этот чат (если список пуст — работаем везде)."""
    if not ALLOWED_CHAT_IDS:
        return True
    return message.chat.id in ALLOWED_CHAT_IDS


# ---------------------------------------------------------------------------
# Привязка ссылок к заданиям (сопоставление с сообщениями)
# ---------------------------------------------------------------------------

def _subject_keywords(subject: str) -> list:
    """Ключевые слова для поиска упоминания предмета в тексте сообщения."""
    mapping = {
        "математика": ["матем", "матеш", "мат-ка", "алгебр", "геометр",
                       "номер", "числа", "пример"],
        "русский язык": ["русск", "рус яз", "упражнен", "упр ", "правил"],
        "литература": ["литра", "лит-ра", "литератур", "стих", "рассказ",
                       "сочинен"],
        "физика": ["физик"],
        "химия": ["химия", "химик", "уравнен", "реакц"],
        "биология": ["биолог", "библ", "цат"],
        "история": ["истор"],
        "обществознание": ["обществ"],
        "география": ["геогр", "атлас", "контурн"],
        "информатика": ["информат", "инф ", "програм", "паскаль", "python"],
        "английский язык": ["англ", "english", "транслейт"],
        "музыка": ["муз"],
        "изо": ["изо", "рисун", "нарисовать"],
        "технология": ["технолог", "труд", "поделк"],
    }
    for key, keywords in mapping.items():
        if key in subject or subject in key:
            return keywords
    return [subject.split()[0][:4]] if subject.split() else []


def _attach_media_links(homework: list, messages: list) -> list:
    """
    Для каждого задания ищет в чате сообщение с медиа и подставляет ссылку.
    Простая эвристика: ищем упоминание предмета → если в том же сообщении
    или рядом было фото — берём ссылку.
    """
    result = []
    for item in homework:
        item = dict(item)
        subject_lower = item.get("subject", "").lower()

        # Ищем сообщение, связанное с предметом
        for msg in messages:
            msg_text = msg.get("text", "").lower()
            if subject_lower and any(
                kw in msg_text for kw in _subject_keywords(subject_lower)
            ):
                if msg.get("has_media"):
                    item["link"] = get_message_link(
                        msg["chat_id"], msg["message_id"]
                    )
                break
        else:
            # Нет прямого совпадения — берём последнее сообщение с медиа
            for msg in reversed(messages):
                if msg.get("has_media"):
                    item["link"] = get_message_link(
                        msg["chat_id"], msg["message_id"]
                    )
                    break

        result.append(item)
    return result


# ---------------------------------------------------------------------------
# Сохранение сообщения в базу
# ---------------------------------------------------------------------------

async def save_to_database(message: Message) -> None:
    """Сохраняет сообщение в SQLite (все сообщения, не только Д/З)."""
    if not check_chat(message):
        return

    has_media, media_type = 0, None
    if message.photo:
        has_media, media_type = 1, "фото"
    elif message.document:
        has_media, media_type = 1, "документ"
    elif message.voice or message.audio:
        has_media, media_type = 1, "голосовое"
    elif message.video or message.video_note:
        has_media, media_type = 1, "видео"
    elif message.animation:
        has_media, media_type = 1, "gif"
    elif message.sticker:
        has_media, media_type = 1, "стикер"

    text = (message.text or message.caption or "").strip()

    msg_data = {
        "message_id": message.message_id,
        "chat_id": message.chat.id,
        "user_id": message.from_user.id if message.from_user else None,
        "username": (
            message.from_user.full_name
            if message.from_user and message.from_user.full_name
            else (message.from_user.username if message.from_user else "Без имени")
        ),
        "text": text,
        "date": message.date.strftime("%Y-%m-%d"),
        "has_media": has_media,
        "media_type": media_type,
        "media_link": (
            get_message_link(message.chat.id, message.message_id)
            if has_media else None
        ),
    }
    await save_message(msg_data)


# ---------------------------------------------------------------------------
# Единый обработчик всех текстовых сообщений
# ---------------------------------------------------------------------------

HELP_TEXT = (
    "Привет! Я бот для сбора домашних заданий 📚\n\n"
    "Я просто слушаю чат и запоминаю все сообщения.\n\n"
    "Команды:\n"
    "▫️ <b>бот дз</b> — Д/З за сегодня\n"
    "▫️ <b>бот дз вчера</b> — за вчера\n"
    "▫️ <b>бот дз 12 сентября</b> — за конкретную дату"
)


@router.message()
async def on_every_message(message: Message) -> None:
    """
    Главный обработчик: определяет тип сообщения и вызывает нужную логику.
    Один обработчик для всего — так гарантированно не пропускаем ни одно сообщение.
    """
    # Служебные сообщения (new_chat_members, left_chat_member и т.д.)
    # могут не иметь text/caption — просто игнорируем их.
    raw_text = message.text or message.caption
    if raw_text is None:
        return

    # Не сохраняем собственные сообщения бота (например, его же ответы с Д/З)
    if message.from_user and message.from_user.id == bot.id:
        return

    text_lower = raw_text.strip().lower()

    # ── Команда «бот дз …» ────────────────────────────────────────────
    if text_lower.startswith("бот дз"):
        await _handle_homework(message, text_lower)
        return

    # ── Команда «бот тест» — проверка ключа GigaChat ───────────────────
    if text_lower.startswith("бот тест"):
        await _handle_test(message)
        return

    # ── Команды-справки ────────────────────────────────────────────────
    if text_lower in ("помощь", "help", "старт", "start", "/help", "/start"):
        if message.chat.type == "private":
            await message.reply(HELP_TEXT)
        else:
            # В группе — только если кто-то написал «помощь»
            if text_lower in ("помощь", "/help"):
                await message.reply(HELP_TEXT)
        return

    # ── Обычное сообщение → сохраняем в базу ───────────────────────────
    await save_to_database(message)


# ---------------------------------------------------------------------------
# Приветствие при добавлении бота в группу
# ---------------------------------------------------------------------------

@router.my_chat_member()
async def on_bot_added(update: ChatMemberUpdated) -> None:
    """
    Бота добавили в чат (или выдали права администратора).
    Это событие Telegram присылает даже при включённом privacy mode,
    поэтому пользователь сразу видит, что бот в группе работает.
    """
    # Интересуемся только группой и только самим ботом
    if update.chat.type not in ("group", "supergroup"):
        return

    new_status = update.new_chat_member.status
    old_status = update.old_chat_member.status

    # Срабатываем, когда бота добавили или назначили администратором
    if new_status in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR) and \
       old_status not in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR):

        chat_id = update.chat.id
        is_admin = new_status == ChatMemberStatus.ADMINISTRATOR

        text = (
            "Привет 👋 Я бот для сбора домашних заданий.\n\n"
            f"ID этого чата: <code>{chat_id}</code>\n"
            "Можно вписать его в <b>.env</b> → ALLOWED_CHAT_IDS.\n\n"
        )
        if is_admin:
            text += "✅ Я администратор — вижу все сообщения, Д/З буду собирать.\n\n"
        else:
            text += (
                "⚠️ Я добавлен без прав администратора.\n"
                "В <i>режиме приватности</i> бот не видит обычные сообщения.\n"
                "Сделайте одно из двух:\n"
                "1. Выдайте мне права администратора в настройках чата, ИЛИ\n"
                "2. Напишите @BotFather → /setprivacy → выберите меня → Disable.\n\n"
            )
        text += "Проверить меня: напишите в чате «бот тест» или «бот дз»."

        logger.info(f"Бот добавлен в чат {chat_id} (admin={is_admin})")
        await bot.send_message(chat_id, text)


# ---------------------------------------------------------------------------
# Логика команды «бот тест»
# ---------------------------------------------------------------------------

async def _handle_test(message: Message) -> None:
    """Проверяет доступ к GigaChat и показывает пользователю результат."""
    if not check_chat(message):
        await message.reply("Извините, я работаю только в своём классном чате 🙈")
        return

    msg = await message.reply("🔧 Проверяю ключ GigaChat…")
    ok, detail = await test_connection(GIGACHAT_API_KEY)
    detail_escaped = html.escape(detail)
    reply = "🔧 <b>Тест GigaChat</b>\n\n" + detail_escaped
    if ok:
        reply += "\n\n🎉 Всё работает! Можно писать «бот дз»."
    else:
        reply += (
            "\n\n❌ Похоже, ключ или настройки неверны.\n"
            "Проверьте в .env:\n"
            "• GIGACHAT_API_KEY — ключ из профиля разработчика Сбера\n"
            "• GIGACHAT_SCOPE — GIGACHAT_API_PERS (личный) или GIGACHAT_API_B2B (B2B)"
        )
    try:
        await msg.edit_text(reply)
    except Exception:
        await message.reply(reply)


# ---------------------------------------------------------------------------
# Логика команды «бот дз»
# ---------------------------------------------------------------------------

async def _handle_homework(message: Message, text_lower: str) -> None:
    """Обработка «бот дз [дата]»."""

    if not check_chat(message):
        await message.reply("Извините, я работаю только в своём классном чате 🙈")
        return

    # Вырезаем дату из текста
    date_part = text_lower[6:].strip()               # после «бот дз»
    date_part = re.sub(r"^дата\s*", "", date_part)    # «дата 12 сентября» → «12 сентября»

    target = parse_russian_date(date_part) if date_part else datetime.now()
    if target is None:
        await message.reply(
            "Не понял дату 🤔\n"
            "Примеры: «бот дз», «бот дз вчера», «бот дз 12 сентября»."
        )
        return

    date_str = target.strftime("%Y-%m-%d")                        # для запроса к БД
    date_display = f"{target.day} {MONTHS_NOMINATIVE[target.month]}"  # для человека

    await message.reply("🔍 Ищу Д/З, минуточку…")

    try:
        messages = await get_messages_by_date(message.chat.id, date_str)

        if not messages:
            await message.reply(
                f"📚 Д/З на {date_display}\n\nСообщений за этот день не найдено 😕"
            )
            return

        homework = await process_messages(messages, GIGACHAT_API_KEY)
        homework = _attach_media_links(homework, messages)

        reply = format_homework(homework, date_display)

        if len(reply) > MAX_REPLY_LENGTH:
            reply = reply[:MAX_REPLY_LENGTH]

        await message.reply(reply)

    except Exception as e:
        logger.exception("Ошибка при обработке команды «бот дз»")
        hint = ""
        if "402" in str(e):
            hint = (
                "\n\n💡 Судя по ошибке 402 «Payment Required», у вашего ключа GigaChat"
                " закончились бесплатные токены. Выпустите ЛИЧНЫЙ ключ (PERS) в кабинете"
                " Сбера и поставьте в .env: GIGACHAT_SCOPE=GIGACHAT_API_PERS."
            )
        elif "scope" in str(e).lower():
            hint = (
                "\n\n💡 Ошибка scope: проверьте в .env, что GIGACHAT_SCOPE совпадает"
                " с типом ключа (PERS или B2B)."
            )
        await message.reply(
            "Что-то пошло не так 😕\n"
            f"Ошибка: {html.escape(str(e)[:300])}{hint}"
        )


# ---------------------------------------------------------------------------
# Запуск
# ---------------------------------------------------------------------------

async def main() -> None:
    """Стартовая точка: БД → обработчики → поллинг."""
    await init_db()
    dp.include_router(router)
    logger.info("Бот запущен. Ждём сообщения из чата…")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
