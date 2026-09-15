import asyncio
from fastapi import FastAPI, Request
from aiogram import types
from bot import bot, dp  # Импортируем готовые объекты бота и диспетчера из bot.py

app = FastAPI()

@app.post("/webhook")
async def telegram_webhook(request: Request):
    # Получаем обновление от серверов Telegram
    json_str = await request.json()
    update = types.Update.model_validate(json_str, context={"bot": bot})
    
    # Отдаем его aiogram на обработку
    await dp.feed_update(bot, update)
    return {"status": "ok"}

@app.get("/")
def read_root():
    return {"status": "Бот Homework-bot успешно запущен на Vercel!"}
