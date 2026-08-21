"""Единая точка подключения к SQLite.

Базу пишут три процесса: этот сервис, воркер по таймеру и основной backend.
С настройками по умолчанию это ломается: журнал `delete` блокирует базу
целиком на время записи, а таймаут ожидания 5 секунд. Обновление стадий
из Битрикса длится дольше — и в это время админка и основной сервис
получают «database is locked».

Поэтому здесь:

* **WAL** — читатели не блокируют писателя, а писатель не блокирует
  читателей. Режим записывается в сам файл базы один раз и действует для
  всех процессов, включая основной сервис.
* **busy_timeout** — вместо мгновенной ошибки соединение ждёт освобождения
  блокировки. Пишем мы редко и короткими порциями, так что ожидание в
  доли секунды предпочтительнее исключения.

Раньше подключение дублировалось в четырёх модулях, и настройки пришлось бы
править в четырёх местах — что и было причиной, по которой их не было нигде.
"""
import logging

from .config import settings

# Динамический выбор БД: SQLite (dev) или PostgreSQL через pgshim (прод).
# Тот же паттерн что в основном backend (favorit-app/backend/app/db.py).
# Весь остальной код admin (sqlite-совместимый SQL) работает через
# один и тот же интерфейс `sqlite3` — не требует переписывания.
if settings.use_postgres:
    from . import pgshim as sqlite3  # type: ignore
else:
    import sqlite3  # type: ignore

log = logging.getLogger(__name__)

# Сколько ждать освобождения блокировки. Порции записи короткие, так что
# до этого предела дело доходить не должно — он на случай, когда основной
# сервис делает что-то долгое.
BUSY_TIMEOUT_MS = 15_000

_wal_ready = False


def connect():
    """Соединение с общими настройками. Использовать вместо sqlite3.connect.

    В PG режиме — соединение через pgshim (те же методы, что у sqlite3).
    PRAGMA / WAL не применяются — в PG это управляется на уровне сервера.
    """
    if settings.use_postgres:
        # pgshim.connect игнорирует timeout и row_factory (эмулирует их сам)
        return sqlite3.connect(settings.pg_dsn)

    global _wal_ready
    c = sqlite3.connect(settings.db_path, timeout=BUSY_TIMEOUT_MS / 1000)
    c.row_factory = sqlite3.Row
    c.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    if not _wal_ready:
        try:
            mode = c.execute("PRAGMA journal_mode = WAL").fetchone()[0]
            if str(mode).lower() != "wal":
                log.warning("Не удалось включить WAL, режим журнала: %s", mode)
            _wal_ready = True
        except sqlite3.Error as e:
            # Например, база на сетевом диске. Работать можно и без WAL,
            # просто параллельная запись будет ждать дольше.
            log.warning("WAL недоступен: %s", e)
            _wal_ready = True
    return c
