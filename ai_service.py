"""
Модуль нейросети GigaChat.
Анализирует сообщения из чата и определяет, какие из них содержат домашнее задание.

Используется GigaChat API от Сбера — бесплатно и работает на территории России,
без VPN.

Важно про ключ:
  • У личного ключа (PERS) scope = GIGACHAT_API_PERS.
  • У корпоративного ключа (B2B) scope = GIGACHAT_API_B2B.
  Это значение задаётся в .env, переменная GIGACHAT_SCOPE.
  Если его не задать, библиотека по умолчанию использует PERS — и для B2B-ключа
  OAuth-сервер вернёт ошибку «scope from db not fully includes consumed scope».
"""

import json
import logging
import os
from typing import Dict, List, Tuple

from dotenv import load_dotenv
from gigachat import GigaChatAsyncClient

logger = logging.getLogger(__name__)

# Читаем параметры GigaChat из .env (с разумными значениями по умолчанию)
load_dotenv()

# Тип ключа: GIGACHAT_API_PERS или GIGACHAT_API_B2B — критично для входа в OAuth!
GIGACHAT_SCOPE = os.getenv("GIGACHAT_SCOPE", "GIGACHAT_API_PERS")
# Модель, которую будем использовать. Обязательный параметр для библиотеки gigachat.
# Для корпоративных (B2B) ключей доступны: GigaChat-2, GigaChat-2-Pro,
# GigaChat-2-Max, GigaChat-3-Lightning, GigaChat-3-Pro.
GIGACHAT_MODEL = os.getenv("GIGACHAT_MODEL", "GigaChat-2-Pro")


# Инструкция для нейросети — как распознавать домашние задания
SYSTEM_PROMPT = """Ты — ассистент школьного чата. Твоя задача — проанализировать
список сообщений из группового чата и определить, какие из них содержат
домашнее задание (Д/З).

Для каждого сообщения с домашним заданием определи:
1. Предмет: русский язык, математика, алгебра, геометрия, литература, физика,
   химия, биология, история, обществознание, география, информатика,
   английский язык, алгоритмика, музыка, ИЗО, технология, физкультура, ОБЖ.
   Если предмет понятен по контексту соседних сообщений — используй его.
2. Текст задания в нормальном, чистом виде: без сленга, опечаток и лишних слов.

ВАЖНЫЕ ПРАВИЛА:
- Отсеивай болтовню, мемы, приветствия, обсуждения, вопросы — это не Д/З.
- Если сообщение типа «номера 12–15» идёт сразу после сообщения «по геометрии»,
  свяжи их: предмет = Геометрия, задание = номера 12–15.
- Исправляй опечатки: «задали по матеше номера 245 и 246» → «№245, №246».
- Одно сообщение может содержать задание по нескольким предметам — раздели их.

ОТВЕЧАЙ СТРОГО В ФОРМАТЕ JSON-массива:
[
  {"subject": "Математика", "text": "№245, №246 (стр. 58)"},
  {"subject": "Русский язык", "text": "упражнение 112, выучить правило"}
]

Если домашнего задания нет — выведи пустой массив: []

Никакого текста кроме JSON — только сам массив."""

# Сколько сообщений максимум отправляем нейросети за раз.
# GigaChat Free имеет лимит токенов на запрос, поэтому большие дни
# разбиваются на порции с перекрытием для контекста.
MAX_MESSAGES_PER_BATCH = 40


async def process_messages(messages: List[Dict], api_key: str) -> List[Dict]:
    """
    Принимает список сообщений за день и возвращает список домашних заданий:
    [{"subject": "Математика", "text": "№245, №246"}, ...]

    Аргументы:
        messages — список сообщений из базы данных
        api_key — ключ GigaChat API
    """
    if not messages:
        return []

    ALL_RESULTS: List[Dict] = []
    seen = set()

    # Если сообщений много — обрабатываем порциями по 40,
    # с перекрытием в 5 сообщений, чтобы не терять контекст.
    batches = [
        messages[i:i + MAX_MESSAGES_PER_BATCH]
        for i in range(0, len(messages), MAX_MESSAGES_PER_BATCH - 5)
    ]

    for batch in batches:
        user_prompt = _build_prompt(batch)
        response_text = await _ask_gigachat(user_prompt, api_key)
        parsed = _parse_response(response_text)

        # Убираем дубликаты (одно и то же задание могло попасть в две порции)
        for item in parsed:
            key = (item["subject"].strip().lower(), item["text"].strip().lower())
            if key not in seen:
                seen.add(key)
                ALL_RESULTS.append(item)

    return ALL_RESULTS


def _build_prompt(messages: List[Dict]) -> str:
    """Формирует текст запроса: нумерованный список сообщений с авторами."""
    lines = ["Вот сообщения из школьного чата за день. Определи, какие из них",
             "содержат домашнее задание. Учитывай контекст разговора.",
             "", "---", ""]

    for i, msg in enumerate(messages, start=1):
        author = msg.get("username", "Без имени")
        text = msg.get("text", "")

        # Если есть медиа — указываем это, чтобы нейросеть поняла контекст
        media_part = ""
        if msg.get("has_media"):
            media_part = f" [в сообщении {msg.get('media_type', 'медиа')}]"

        # Обрезаем очень длинные сообщения — 500 символов достаточно
        if len(text) > 500:
            text = text[:500] + "…"

        lines.append(f"[{i}] {author}: {text}{media_part}")

    lines.extend(["", "---", "",
                  "Верни JSON-массив с домашними заданиями. Если Д/З нет — верни []."])
    return "\n".join(lines)


async def _ask_gigachat(user_prompt: str, api_key: str) -> str:
    """Отправляет запрос в GigaChat и возвращает текст ответа."""
    try:
        async with GigaChatAsyncClient(
            credentials=api_key,
            scope=GIGACHAT_SCOPE,          # PERS или B2B — критично для вашего ключа
            model=GIGACHAT_MODEL,          # модель обязательна для этой библиотеки
            verify_ssl_certs=False,        # GigaChat использует свой сертификат
            timeout=90.0,
        ) as giga:
            response = await giga.achat({
                "model": GIGACHAT_MODEL,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
            })
            return response.choices[0].message.content

    except Exception as e:
        logger.error(f"Ошибка при обращении к GigaChat: {e}")
        raise


async def test_connection(api_key: str) -> Tuple[bool, str]:
    """
    Проверяет ключ GigaChat и текущие настройки.
    Отправляет реальный запрос к модели (самый честный тест).
    Возвращает (True, сообщение) при успехе или (False, текст ошибки).
    """
    try:
        async with GigaChatAsyncClient(
            credentials=api_key,
            scope=GIGACHAT_SCOPE,
            model=GIGACHAT_MODEL,
            verify_ssl_certs=False,
            timeout=60.0,
        ) as giga:
            response = await giga.achat({
                "model": GIGACHAT_MODEL,
                "messages": [
                    {"role": "user", "content": "Ответь одним словом: работает ли у тебя связь?"},
                ],
                "max_tokens": 10,
            })
            answer = response.choices[0].message.content
        return (
            True,
            f"Ключ рабочий ✅\nМодель <b>{GIGACHAT_MODEL}</b> ответила: «{answer.strip()[:50]}»\n"
            f"Scope: {GIGACHAT_SCOPE}",
        )
    except Exception as e:
        err = str(e)
        if "402" in err:
            hint = (
                "\n\n❌ Ключу не хватает платных токенов (402 Payment Required).\n"
                "Выпустите ЛИЧНЫЙ ключ GigaChat (PERS) — у него есть бесплатный пакет,\n"
                "и поставьте в .env: GIGACHAT_SCOPE=GIGACHAT_API_PERS."
            )
        elif "scope" in err.lower():
            hint = (
                "\n\n❌ Не совпадает тип ключа. Поставьте в .env:\n"
                "• GIGACHAT_SCOPE=GIGACHAT_API_PERS — если ключ личный\n"
                "• GIGACHAT_SCOPE=GIGACHAT_API_B2B — если ключ корпоративный"
            )
        else:
            hint = "\n\nПроверьте GIGACHAT_API_KEY в .env."
        return False, f"Ошибка: {err[:300]}{hint}"


def _parse_response(response_text: str) -> List[Dict]:
    """
    Разбирает ответ нейросети в список заданий.
    Если ответ не является валидным JSON — возвращаем пустой список.
    """
    text = response_text.strip()

    # Нейросеть иногда заворачивает JSON в markdown-блоки ```json ... ```
    if "```" in text:
        parts = text.split("```")
        for part in parts:
            part = part.strip().lstrip("json").strip()
            if part.startswith("["):
                text = part
                break

    try:
        data = json.loads(text)
        if not isinstance(data, list):
            return []
        # Фильтруем мусор: нужны только словари с предметом и текстом
        result = []
        for item in data:
            if isinstance(item, dict) and item.get("subject") and item.get("text"):
                result.append({
                    "subject": str(item["subject"]).strip(),
                    "text": str(item["text"]).strip(),
                })
        return result
    except json.JSONDecodeError:
        logger.warning("Нейросеть вернула невалидный JSON")
        return []