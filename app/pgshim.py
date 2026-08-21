"""
sqlite3-совместимый shim поверх psycopg2.

Задача: позволить существующему коду `db.py` работать с PostgreSQL
без переписывания 100+ SQL-запросов. Импорт `import sqlite3` заменяется
на `from . import pgshim as sqlite3`, дальше всё то же.

Что делает shim:
- `?` placeholders → `%s`
- `INTEGER PRIMARY KEY AUTOINCREMENT` → `SERIAL PRIMARY KEY`
- `INSERT OR IGNORE INTO x (...)` → `INSERT INTO x (...) ... ON CONFLICT DO NOTHING`
- `INSERT OR REPLACE INTO x (...)` → раскрывается через `ON CONFLICT ... DO UPDATE SET ...`
  (используется явно там где нужно — в коде их мало)
- `PRAGMA ...` → no-op (возвращает пустой cursor)
- `CURRENT_TIMESTAMP` — работает в PG нативно
- Row: dict-like + integer indexing, как в sqlite3.Row
- `cursor.lastrowid` — эмулируется через `RETURNING id` в INSERT (для таблиц с id)
- `OperationalError` и другие exception классы — из psycopg2
- Контекстный менеджер `with conn:` — автокоммит/rollback

Что НЕ поддерживается (проверить в коде что не используется):
- attach database
- FTS-таблицы
- executescript() с несколькими statement'ами (используется init() — надо разбить)
"""
from __future__ import annotations

import re
import threading
from typing import Optional, Sequence

import psycopg2
import psycopg2.extras
from psycopg2 import DatabaseError, ProgrammingError
from psycopg2 import errors as pg_errors


# Псевдонимы sqlite3-исключений над psycopg2. В коде часто ловят
# `except sqlite3.OperationalError:` для ALTER TABLE ADD COLUMN (когда
# колонка уже есть) — в PG это `DuplicateColumn`. `IntegrityError`
# в PG уже такой класс существует (unique/foreign key violations).
IntegrityError = psycopg2.IntegrityError

# Ловим широкий набор Programming/Operational ошибок PG под именем
# OperationalError. В коде это используется только для «безопасных»
# ALTER TABLE ADD COLUMN — где ошибка означает «колонка уже есть»,
# и её надо тихо проглотить.
OperationalError = ProgrammingError

Error = DatabaseError


# ---------------------- SQL transformation ----------------------

_RE_PLACEHOLDER = re.compile(r'\?')
_RE_AUTOINCREMENT = re.compile(
    r'INTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT', re.IGNORECASE
)
_RE_INSERT_OR_IGNORE = re.compile(r'INSERT\s+OR\s+IGNORE\s+INTO', re.IGNORECASE)
_RE_PRAGMA = re.compile(r'^\s*PRAGMA\b', re.IGNORECASE)
_RE_INSERT_INTO_TABLE = re.compile(
    r'INSERT\s+INTO\s+([a-zA-Z_][a-zA-Z0-9_]*)', re.IGNORECASE
)


# Кэш: {table_name: True/False} — есть ли колонка id для RETURNING.
# Заполняется автоматически на первой попытке INSERT ... RETURNING id.
_has_id_cache: dict[str, bool] = {}
_cache_lock = threading.Lock()


_RE_ALTER_ADD_COLUMN = re.compile(
    r'ALTER\s+TABLE\s+(\S+)\s+ADD\s+COLUMN\s+(?!IF\s+NOT\s+EXISTS)', re.IGNORECASE
)


def _translate_sql(sql: str) -> str:
    """Преобразовать SQLite-стиль SQL в PostgreSQL-совместимый."""
    sql = _RE_PLACEHOLDER.sub('%s', sql)
    sql = _RE_AUTOINCREMENT.sub('SERIAL PRIMARY KEY', sql)
    # INSERT OR IGNORE INTO x (cols) VALUES (?) → INSERT INTO x (cols) VALUES (?) ON CONFLICT DO NOTHING
    if _RE_INSERT_OR_IGNORE.search(sql):
        sql = _RE_INSERT_OR_IGNORE.sub('INSERT INTO', sql)
        # добавляем ON CONFLICT DO NOTHING в конец (перед ; если есть)
        if 'ON CONFLICT' not in sql.upper():
            sql = sql.rstrip().rstrip(';') + ' ON CONFLICT DO NOTHING'
    # ALTER TABLE x ADD COLUMN y ... → ALTER TABLE x ADD COLUMN IF NOT EXISTS y ...
    # (в исходных try/except коде это ожидает OperationalError при дубле — теперь просто no-op).
    sql = _RE_ALTER_ADD_COLUMN.sub(r'ALTER TABLE \1 ADD COLUMN IF NOT EXISTS ', sql)
    return sql


# ---------------------- Row wrapper ----------------------

class _Row:
    """Эмулирует sqlite3.Row: доступ по int-индексу И по имени колонки.
    Поддерживает `dict(row)`, `row.keys()`, `row["col"]`, `row[0]`.
    """
    __slots__ = ('_data', '_keys')

    def __init__(self, keys: tuple[str, ...], values: tuple):
        self._keys = keys
        self._data = values

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._data[key]
        try:
            idx = self._keys.index(key)
        except ValueError as e:
            raise IndexError(f"no such key: {key}") from e
        return self._data[idx]

    def __len__(self):
        return len(self._data)

    def __iter__(self):
        return iter(self._data)

    def keys(self):
        return list(self._keys)

    def get(self, key, default=None):
        try:
            return self[key]
        except (IndexError, KeyError):
            return default

    def __contains__(self, key):
        return key in self._keys

    def __repr__(self):
        return f"<Row {dict(zip(self._keys, self._data))}>"


# ---------------------- Cursor ----------------------

class _Cursor:
    def __init__(self, real_cursor, conn: '_Connection'):
        self._cur = real_cursor
        self._conn = conn
        self._lastrowid: Optional[int] = None

    _sp_counter = 0

    def _next_sp(self) -> str:
        _Cursor._sp_counter += 1
        return f"sp_{_Cursor._sp_counter}"

    def _safe_exec(self, sql: str, params: Sequence = ()):
        """Выполнить SQL внутри SAVEPOINT — при ошибке откатить ТОЛЬКО этот
        statement, оставив остальную транзакцию нетронутой. SAVEPOINT/RELEASE
        через отдельный cursor, чтобы не сбросить результаты self._cur."""
        sp = self._next_sp()
        aux = self._conn._real.cursor()
        try:
            aux.execute(f"SAVEPOINT {sp}")
            try:
                self._cur.execute(sql, params)
                aux.execute(f"RELEASE SAVEPOINT {sp}")
            except Exception:
                aux.execute(f"ROLLBACK TO SAVEPOINT {sp}")
                aux.execute(f"RELEASE SAVEPOINT {sp}")
                raise
        finally:
            aux.close()

    def execute(self, sql: str, params: Sequence = ()):
        # PRAGMA — no-op
        if _RE_PRAGMA.match(sql):
            return self

        sql_t = _translate_sql(sql)
        params = params or ()

        # Если это INSERT INTO tbl — попробуем добавить RETURNING id
        # для эмуляции lastrowid.
        added_returning = False
        m = _RE_INSERT_INTO_TABLE.match(sql_t.lstrip())
        if m:
            tbl = m.group(1).lower()
            has_id = _has_id_cache.get(tbl)

            if has_id is True and 'RETURNING' not in sql_t.upper():
                sql_t = sql_t.rstrip().rstrip(';') + ' RETURNING id'
                self._safe_exec(sql_t, params)
                added_returning = True
            elif has_id is None and 'RETURNING' not in sql_t.upper():
                # Первый раз для этой таблицы — пробуем с RETURNING в SAVEPOINT.
                try:
                    self._safe_exec(sql_t.rstrip().rstrip(';') + ' RETURNING id', params)
                    with _cache_lock:
                        _has_id_cache[tbl] = True
                    added_returning = True
                except (pg_errors.UndefinedColumn, ProgrammingError):
                    with _cache_lock:
                        _has_id_cache[tbl] = False
                    self._safe_exec(sql_t, params)
            else:
                self._safe_exec(sql_t, params)
        else:
            self._safe_exec(sql_t, params)

        # Сохранить lastrowid, если RETURNING id
        if added_returning:
            try:
                row = self._cur.fetchone()
                if row and row[0] is not None:
                    self._lastrowid = int(row[0])
                else:
                    # ON CONFLICT DO NOTHING — вставки не было
                    self._lastrowid = None
            except (psycopg2.ProgrammingError, TypeError):
                self._lastrowid = None
        return self

    def executemany(self, sql: str, params_list):
        sql_t = _translate_sql(sql)
        # executemany + RETURNING не совместимы легко, пропускаем автоматическое RETURNING
        self._cur.executemany(sql_t, list(params_list))
        return self

    def fetchone(self) -> Optional[_Row]:
        row = self._cur.fetchone()
        if row is None:
            return None
        keys = tuple(d[0] for d in (self._cur.description or ()))
        return _Row(keys, tuple(row))

    def fetchall(self) -> list[_Row]:
        rows = self._cur.fetchall()
        keys = tuple(d[0] for d in (self._cur.description or ()))
        return [_Row(keys, tuple(r)) for r in rows]

    def __iter__(self):
        return self

    def __next__(self):
        r = self.fetchone()
        if r is None:
            raise StopIteration
        return r

    @property
    def lastrowid(self) -> Optional[int]:
        return self._lastrowid

    @property
    def rowcount(self) -> int:
        return self._cur.rowcount

    @property
    def description(self):
        return self._cur.description

    def close(self):
        try:
            self._cur.close()
        except Exception:
            pass


# ---------------------- Connection ----------------------

class _Connection:
    def __init__(self, dsn: str):
        self._real = psycopg2.connect(dsn)
        # sqlite3 по умолчанию auto-BEGIN + explicit commit; psycopg2 то же самое,
        # так что оставляем autocommit=False.
        self._real.autocommit = False
        # row_factory — заглушка (в sqlite3 это влияло на возвращаемый тип rows;
        # у нас _Row делает и dict-like, и index-access, так что параметр не нужен)
        self.row_factory = None

    def execute(self, sql: str, params: Sequence = ()):
        cur = _Cursor(self._real.cursor(), self)
        cur.execute(sql, params)
        return cur

    def executemany(self, sql: str, params_list):
        cur = _Cursor(self._real.cursor(), self)
        cur.executemany(sql, params_list)
        return cur

    def cursor(self):
        return _Cursor(self._real.cursor(), self)

    def commit(self):
        self._real.commit()

    def rollback(self):
        self._real.rollback()

    def close(self):
        try:
            self._real.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is not None:
            self.rollback()
        else:
            self.commit()
        # sqlite3 контекстный менеджер НЕ закрывает соединение — только commit/rollback
        return False


# ---------------------- Public API ----------------------

def connect(dsn: str, timeout: float | None = None, **kwargs) -> _Connection:
    """
    Эмуляция sqlite3.connect(). `dsn` — либо путь (для совместимости), либо
    PG connection string. Если путь — читаем настоящий DSN из settings.
    `timeout` игнорируется (у psycopg2 своя настройка connect_timeout).
    """
    # Если это путь к файлу sqlite (устаревшее использование) — берём PG DSN из settings.
    if not dsn.startswith(('postgres://', 'postgresql://', 'host=', 'dbname=')):
        from .config import settings
        dsn = settings.pg_dsn
    return _Connection(dsn)


# Для обратной совместимости с `sqlite3.Row` (нам не важно точное поведение)
class Row:
    pass
