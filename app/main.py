"""Admin-panel service — изолированный от основного backend.

Читает/пишет ту же SQLite `favorit.db`, что и главный сервис, но живёт
как отдельный процесс на порту 8001. Nginx роутит /admin/* сюда,
/api/* — на главный (8000). Так админ-разработчик работает в своём
репо, никогда не касаясь клиентского backend или Flutter-приложения.
"""
import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import complaints, db, quality, stages
from .admin import router as admin_router
from .config import settings

logging.basicConfig(level=logging.INFO)

# Инициализация схемы БД — идемпотентна (CREATE IF NOT EXISTS + ALTER
# в try/except), поэтому безопасно вызывать в обоих сервисах.
db.init()

# Таблицы, принадлежащие только админ-сервису (контроль качества, рейтинг
# менеджеров). Вынесены из db.init() намеренно: та функция — общая схема,
# и главный сервис вызывает её же. Про эти таблицы он знать не должен.
quality.init()
complaints.init()
stages.init()

app = FastAPI(
    title="Фаворит · Админ-панель",
    description="Изолированный сервис администратора. Читает общую SQLite.",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(admin_router)


@app.get("/health", tags=["health"])
def health():
    """Простая проверка живучести для systemd/мониторинга."""
    return {"status": "ok", "service": "favorit-admin"}
