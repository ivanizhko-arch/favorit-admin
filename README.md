# Фаворит · Админ-панель

Изолированный сервис администратора. Отдельный от основного backend (`favorit-app`), работает на порту **8001**. Nginx маршрутизирует `https://app.favorit-consult.ru/admin/*` сюда, а `/api/*` — на главный сервис.

Читает и пишет ту же SQLite-базу `favorit.db`, что и главный сервис. Данные общие, код разделён.

## Локальная разработка

```bash
# Один раз:
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Отредактируй .env (пароль, путь к БД если нужно)

# Каждый раз:
source .venv/bin/activate
uvicorn app.main:app --reload --port 8001
```

Открой в браузере: **http://127.0.0.1:8001/admin**. Пароль — из `.env` (по умолчанию `admin123`).

## Структура

```
app/
├── main.py          FastAPI-приложение (mount router, CORS, /health)
├── admin.py         Все /admin/api/* эндпоинты
├── db.py            Helpers по SQLite (общие с главным сервисом)
├── config.py        Настройки через pydantic-settings + .env
├── security.py      JWT, get_admin dependency
└── static/
    └── admin.html   Сама страница админки (HTML + inline JS)
```

Если добавляешь новую фичу:
1. **Новый эндпоинт** → `app/admin.py`
2. **Новый запрос в БД** → `app/db.py` (helper-функция)
3. **Новая кнопка/раздел в UI** → `app/static/admin.html`

## Что нельзя менять

`app/db.py` содержит **общую схему БД**. Функция `init()` вызывается и здесь, и в главном сервисе. Если ты добавляешь новую таблицу — согласуй с Иваном, чтобы главный сервис тоже её увидел. Изменение существующих таблиц может сломать клиентский backend.

## Деплой

Ты не деплоишь напрямую. Workflow:

1. `git checkout -b feature/название`
2. Пушишь ветку
3. Открываешь Pull Request в `main`
4. Иван ревьюит + мёржит + запускает деплой на прод

## Тестовый вход в существующую админку

Проверить как выглядит текущий UI: https://app.favorit-consult.ru/admin (пароль у Ивана).
