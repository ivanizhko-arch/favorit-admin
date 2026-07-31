"""
Хранилище пользователей приложения (SQLite).

Сейчас — локальный файл для разработки. На РФ-сервере заменим на PostgreSQL
(тот же интерфейс функций). Здесь ведём: кто вошёл, согласия, активность,
блокировки — всё, чем управляет админ-панель.
"""
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from . import store
from .config import settings


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def norm_phone(s: str) -> str:
    """Нормализация номера: только цифры, последние 10 (без +7/8)."""
    return re.sub(r"\D", "", s or "")[-10:]


def _conn() -> sqlite3.Connection:
    # Настройки соединения общие для всех модулей — см. store.connect().
    return store.connect()


def init() -> None:
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS users (
                email TEXT PRIMARY KEY,
                name TEXT DEFAULT '',
                phone TEXT DEFAULT '',
                first_seen TEXT,
                last_seen TEXT,
                consent_at TEXT,
                blocked INTEGER DEFAULT 0,
                sessions_valid_after INTEGER DEFAULT 0,
                avatar_url TEXT DEFAULT ''
            )
        """)
        # Миграция для существующих БД, где users уже создана без avatar_url.
        try:
            c.execute("ALTER TABLE users ADD COLUMN avatar_url TEXT DEFAULT ''")
        except sqlite3.OperationalError:
            pass  # колонка уже есть
        c.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT,
                kind TEXT,
                at TEXT
            )
        """)
        # Анти-спам / 230-ФЗ
        c.execute("""
            CREATE TABLE IF NOT EXISTS collector_numbers (
                phone TEXT PRIMARY KEY,            -- нормализованный (10 цифр)
                raw TEXT,
                category TEXT DEFAULT 'collector', -- collector | scammer | spam
                status TEXT DEFAULT 'pending',     -- pending | confirmed | rejected
                reports INTEGER DEFAULT 0,
                first_reported TEXT,
                last_reported TEXT
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS collector_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phone TEXT, reporter TEXT, comment TEXT, at TEXT
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS collector_whitelist (
                phone TEXT PRIMARY KEY, label TEXT, at TEXT
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS call_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user TEXT, from_phone TEXT, kind TEXT, at TEXT  -- kind: call | message
            )
        """)
        # Загруженные файлы документов (клиент → приложение).
        c.execute("""
            CREATE TABLE IF NOT EXISTS document_uploads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL,        -- владелец (нормализованный, .lower())
                doc_id TEXT NOT NULL,       -- идентификатор документа (например, title)
                filename TEXT NOT NULL,     -- оригинальное имя файла
                size INTEGER NOT NULL,      -- байт
                mime TEXT DEFAULT '',
                path TEXT NOT NULL,         -- относительный путь на диске
                uploaded_at TEXT NOT NULL
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_uploads_email_doc "
                  "ON document_uploads (email, doc_id)")
        # Локальный кэш чата клиент⇄юрист (Open Channels). Клиентское
        # сообщение сохраняем при отправке. Ответ юриста прилетает
        # исходящим вебхуком OnImConnectorMessageAdd и тоже сохраняется сюда.
        c.execute("""
            CREATE TABLE IF NOT EXISTS chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL,          -- владелец диалога (клиент)
                text TEXT NOT NULL,
                incoming INTEGER NOT NULL,    -- 1 = от юриста, 0 = от клиента
                is_file INTEGER DEFAULT 0,
                file_url TEXT DEFAULT '',
                file_name TEXT DEFAULT '',
                author_name TEXT DEFAULT '',  -- для входящих: имя юриста
                bitrix_msg_id TEXT DEFAULT '', -- дедупликация webhook'ов
                read INTEGER DEFAULT 0,        -- 1 = клиент прочитал (для incoming)
                created_at TEXT NOT NULL
            )
        """)
        # Миграция: если таблица создана до появления колонки read — добавим.
        try:
            c.execute("ALTER TABLE chat_messages ADD COLUMN read INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass  # уже существует
        c.execute("CREATE INDEX IF NOT EXISTS idx_chat_email_id "
                  "ON chat_messages (email, id)")
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_chat_bitrix "
                  "ON chat_messages (bitrix_msg_id) WHERE bitrix_msg_id != ''")
        # События по прожиточному минимуму — клиент отмечает получил/запросил
        # ПМ за конкретный месяц. Хранится локально, чтобы не спамить Битрикс.
        c.execute("""
            CREATE TABLE IF NOT EXISTS pm_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL,
                year_month TEXT NOT NULL,      -- '2026-07'
                kind TEXT NOT NULL,             -- received | consent_requested
                method TEXT DEFAULT '',         -- bank_statement | special_account
                file_url TEXT DEFAULT '',       -- ссылка на выписку (если method=bank_statement)
                bitrix_task_id INTEGER DEFAULT 0,
                created_at TEXT NOT NULL
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_pm_email_ym ON pm_events (email, year_month)")
        # Эскалация «Не получил ПМ» — по регламенту ФАВОРИТ:
        # уровень 1 (клиент нажал «Не получил»)     → задача АУ, дедлайн 3 рабочих дня
        # уровень 2 (день 20 после конца месяца)    → задача руководителю отдела сопровождения (красный тон)
        # уровень 3 (день 23 после конца месяца)    → задача финдиректору + АУ
        # Одна запись на клиента + месяц. resolved_at=NULL пока клиент не отметил «получил».
        c.execute("""
            CREATE TABLE IF NOT EXISTS pm_escalations (
                email TEXT NOT NULL,
                year_month TEXT NOT NULL,           -- '2026-07'
                level INTEGER NOT NULL DEFAULT 1,   -- 1 | 2 | 3
                started_at TEXT NOT NULL,           -- когда нажали «не получил»
                month_end TEXT NOT NULL,            -- последний день отчётного месяца, YYYY-MM-DD
                level_reached_at TEXT NOT NULL,     -- когда пришли на текущий уровень
                task_ids TEXT DEFAULT '',           -- csv id задач в Битриксе
                resolved_at TEXT,                   -- когда клиент отметил «получил»
                PRIMARY KEY (email, year_month)
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_pmesc_unresolved "
                  "ON pm_escalations (resolved_at)")
        # Реферальные заявки от рекомендованных друзей.
        # Клиент делится ref-кодом → друг оставляет заявку на лендинге →
        # мы создаём лид в Битриксе и запоминаем связь для выплаты бонуса.
        c.execute("""
            CREATE TABLE IF NOT EXISTS referrals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ref_code TEXT NOT NULL,             -- код рекомендующего
                referrer_email TEXT NOT NULL,        -- email клиента-автора
                friend_name TEXT NOT NULL,
                friend_phone TEXT NOT NULL,
                friend_email TEXT DEFAULT '',
                debt_amount TEXT DEFAULT '',         -- «до 500 тыс», «500-1 млн», ...
                comment TEXT DEFAULT '',
                bitrix_lead_id INTEGER DEFAULT 0,   -- ID лида в Битриксе (0 если не создался)
                status TEXT DEFAULT 'new',           -- new | converted | rejected
                payout_amount INTEGER DEFAULT 0,     -- сколько выплатили клиенту
                payout_at TEXT DEFAULT '',
                created_at TEXT NOT NULL
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_ref_referrer ON referrals (referrer_email)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_ref_code ON referrals (ref_code)")
        # NPS-оценки клиентов (0-10) после «Долги списаны».
        # Сохраняем только 8-10 (промоутеры) — по требованию бизнеса.
        c.execute("""
            CREATE TABLE IF NOT EXISTS nps_scores (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL,
                score INTEGER NOT NULL,      -- 8, 9 или 10
                left_review INTEGER DEFAULT 0, -- клиент нажал «Оставить отзыв на Яндекс Картах»
                created_at TEXT NOT NULL
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_nps_email ON nps_scores (email)")
        # Отправленные напоминания об оплате (за 3 дня). Уникальность
        # (email, due_date, kind) гарантирует что не пришлём одному клиенту
        # два раза за один и тот же платёж.
        c.execute("""
            CREATE TABLE IF NOT EXISTS payment_reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL,
                due_date TEXT NOT NULL,          -- YYYY-MM-DD
                kind TEXT NOT NULL DEFAULT '3days',
                amount INTEGER DEFAULT 0,
                sent_at TEXT NOT NULL
            )
        """)
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_payrem_uniq "
                  "ON payment_reminders (email, due_date, kind)")
        # OAuth-токены «Локального приложения» Битрикс (singleton — id=1).
        # Обновляются при установке приложения и при рефреше токена.
        c.execute("""
            CREATE TABLE IF NOT EXISTS bitrix_oauth (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                domain TEXT NOT NULL,
                member_id TEXT DEFAULT '',
                access_token TEXT NOT NULL,
                refresh_token TEXT NOT NULL,
                expires_at INTEGER NOT NULL,        -- unix seconds
                scope TEXT DEFAULT '',
                installed_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)


def log_event(email: str, kind: str) -> None:
    with _conn() as c:
        c.execute("INSERT INTO events (email, kind, at) VALUES (?, ?, ?)",
                  (email.lower(), kind, _now_iso()))


def set_avatar_url(email: str, url: str) -> None:
    """Сохранить URL аватара клиента (перезаписывает предыдущий)."""
    with _conn() as c:
        c.execute(
            "INSERT INTO users (email, avatar_url, first_seen, last_seen) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(email) DO UPDATE SET avatar_url = excluded.avatar_url",
            (email.lower(), url, _now_iso(), _now_iso()),
        )


def get_avatar_url(email: str) -> str:
    with _conn() as c:
        r = c.execute(
            "SELECT avatar_url FROM users WHERE email = ?",
            (email.lower(),),
        ).fetchone()
        return (r["avatar_url"] if r else "") or ""


def touch_consent(email: str) -> None:
    """Отметить согласие на обработку ПДн (при запросе кода)."""
    email = email.lower()
    now = _now_iso()
    with _conn() as c:
        c.execute(
            "INSERT INTO users (email, first_seen, last_seen, consent_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(email) DO UPDATE SET consent_at = COALESCE(users.consent_at, ?)",
            (email, now, now, now, now),
        )


def upsert_login(email: str) -> None:
    """Зафиксировать вход пользователя."""
    email = email.lower()
    now = _now_iso()
    with _conn() as c:
        c.execute(
            "INSERT INTO users (email, first_seen, last_seen) VALUES (?, ?, ?) "
            "ON CONFLICT(email) DO UPDATE SET last_seen = ?",
            (email, now, now, now),
        )


def is_blocked(email: str) -> bool:
    with _conn() as c:
        r = c.execute("SELECT blocked FROM users WHERE email = ?", (email.lower(),)).fetchone()
        return bool(r and r["blocked"])


def sessions_valid_after(email: str) -> int:
    with _conn() as c:
        r = c.execute("SELECT sessions_valid_after FROM users WHERE email = ?",
                      (email.lower(),)).fetchone()
        return int(r["sessions_valid_after"]) if r else 0


# ---- Операции админа ----
def _like(query: str) -> str:
    """Шаблон для LIKE с экранированием служебных % и _.
    Без этого поиск по «100_500» вернул бы лишние строки."""
    q = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{q}%"


# Разрешённые сортировки. Белый список — колонка подставляется в SQL строкой,
# параметром её передать нельзя, поэтому произвольное значение недопустимо.
_USER_SORTS = {
    "last_seen": "last_seen DESC",
    "first_seen": "first_seen DESC",
    "email": "email ASC",
    "name": "name ASC",
}


def list_users(
    query: str = "",
    status: str = "",
    sort: str = "last_seen",
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """Поиск пользователей с фильтрами. Возвращает {items, total, limit, offset}.

    status: '' (все) | active | blocked | consented | no_consent
    """
    where: list[str] = []
    args: list = []
    if query:
        where.append("(email LIKE ? ESCAPE '\\' OR name LIKE ? ESCAPE '\\' "
                     "OR phone LIKE ? ESCAPE '\\')")
        like = _like(query)
        args += [like, like, like]
    if status == "active":
        where.append("COALESCE(blocked, 0) = 0")
    elif status == "blocked":
        where.append("blocked = 1")
    elif status == "consented":
        where.append("consent_at IS NOT NULL AND consent_at != ''")
    elif status == "no_consent":
        where.append("(consent_at IS NULL OR consent_at = '')")

    clause = (" WHERE " + " AND ".join(where)) if where else ""
    order = _USER_SORTS.get(sort, _USER_SORTS["last_seen"])
    limit = max(1, min(int(limit), 500))
    offset = max(0, int(offset))

    with _conn() as c:
        total = c.execute(f"SELECT COUNT(*) n FROM users{clause}", args).fetchone()["n"]
        rows = c.execute(
            f"SELECT * FROM users{clause} ORDER BY {order} LIMIT ? OFFSET ?",
            (*args, limit, offset),
        ).fetchall()
    return {"items": [dict(r) for r in rows], "total": int(total),
            "limit": limit, "offset": offset}


def get_user(email: str) -> Optional[dict]:
    with _conn() as c:
        r = c.execute("SELECT * FROM users WHERE email = ?", (email.lower(),)).fetchone()
        if not r:
            return None
        user = dict(r)
        user["events"] = [
            dict(e) for e in c.execute(
                "SELECT kind, at FROM events WHERE email = ? ORDER BY id DESC LIMIT 20",
                (email.lower(),)).fetchall()
        ]
        return user


def set_blocked(email: str, blocked: bool) -> None:
    with _conn() as c:
        c.execute("UPDATE users SET blocked = ? WHERE email = ?",
                  (1 if blocked else 0, email.lower()))


def reset_sessions(email: str) -> None:
    """«Разлогинить» на всех устройствах — токены, выданные раньше, станут недействительны."""
    with _conn() as c:
        c.execute("UPDATE users SET sessions_valid_after = ? WHERE email = ?",
                  (int(time.time()), email.lower()))


def delete_user(email: str) -> None:
    """Полное удаление персональных данных (152-ФЗ, право на забвение)."""
    with _conn() as c:
        c.execute("DELETE FROM events WHERE email = ?", (email.lower(),))
        c.execute("DELETE FROM users WHERE email = ?", (email.lower(),))


# ---- Анти-спам / 230-ФЗ ----
def is_whitelisted(phone: str) -> bool:
    p = norm_phone(phone)
    with _conn() as c:
        r = c.execute("SELECT 1 FROM collector_whitelist WHERE phone = ?", (p,)).fetchone()
        return r is not None


def add_whitelist(phone: str, label: str) -> None:
    p = norm_phone(phone)
    with _conn() as c:
        c.execute(
            "INSERT INTO collector_whitelist (phone, label, at) VALUES (?, ?, ?) "
            "ON CONFLICT(phone) DO UPDATE SET label = ?",
            (p, label, _now_iso(), label),
        )
        # Если номер был в базе коллекторов — снимаем блок.
        c.execute("UPDATE collector_numbers SET status = 'rejected' WHERE phone = ?", (p,))


def list_whitelist() -> list:
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT phone, label, at FROM collector_whitelist ORDER BY at DESC").fetchall()]


def report_collector(raw: str, category: str, reporter: str, comment: str = "") -> dict:
    """Жалоба на номер. Защищённые номера отклоняются. При достижении порога — статус confirmed."""
    p = norm_phone(raw)
    if not p or len(p) < 10:
        return {"ok": False, "error": "Некорректный номер"}
    if is_whitelisted(p):
        return {"ok": False, "error": "Этот номер в защищённом списке и не может быть заблокирован"}
    now = _now_iso()
    threshold = settings.collector_confirm_threshold
    with _conn() as c:
        c.execute(
            "INSERT INTO collector_numbers (phone, raw, category, status, reports, first_reported, last_reported) "
            "VALUES (?, ?, ?, 'pending', 1, ?, ?) "
            "ON CONFLICT(phone) DO UPDATE SET reports = reports + 1, last_reported = ?",
            (p, raw, category, now, now, now),
        )
        c.execute("INSERT INTO collector_reports (phone, reporter, comment, at) VALUES (?, ?, ?, ?)",
                  (p, norm_phone(reporter) or reporter, comment, now))
        row = c.execute("SELECT reports FROM collector_numbers WHERE phone = ?", (p,)).fetchone()
        reports = row["reports"]
        status = "confirmed" if reports >= threshold else "pending"
        c.execute("UPDATE collector_numbers SET status = ? WHERE phone = ? AND status != 'rejected'",
                  (status, p))
    return {"ok": True, "phone": p, "reports": reports, "status": status,
            "blocked": status == "confirmed"}


def get_blocklist() -> list:
    """Подтверждённые номера коллекторов (для синхронизации на устройство). Белый список исключён."""
    with _conn() as c:
        rows = c.execute(
            "SELECT phone, category FROM collector_numbers "
            "WHERE status = 'confirmed' AND phone NOT IN (SELECT phone FROM collector_whitelist)"
        ).fetchall()
        return [dict(r) for r in rows]


def lookup_number(phone: str) -> dict:
    """Определитель: что известно о номере."""
    p = norm_phone(phone)
    if is_whitelisted(p):
        with _conn() as c:
            r = c.execute("SELECT label FROM collector_whitelist WHERE phone = ?", (p,)).fetchone()
        return {"phone": p, "known": True, "safe": True, "label": r["label"] if r else "Защищённый номер"}
    with _conn() as c:
        r = c.execute("SELECT category, status, reports FROM collector_numbers WHERE phone = ?", (p,)).fetchone()
    if not r:
        return {"phone": p, "known": False}
    return {"phone": p, "known": True, "safe": False,
            "category": r["category"], "status": r["status"], "reports": r["reports"]}


_COLLECTOR_SORTS = {
    "reports": "reports DESC, last_reported DESC",
    "last_reported": "last_reported DESC",
    "phone": "phone ASC",
}


def list_collectors(
    status: str = "",
    query: str = "",
    category: str = "",
    sort: str = "reports",
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """Номера коллекторов для модерации. Возвращает {items, total, limit, offset}.

    Поиск по номеру терпим к формату ввода: «+7 (999) 123-45-67» и «9991234567»
    ищут одно и то же, потому что в базе телефон лежит нормализованным.
    """
    where: list[str] = []
    args: list = []
    if status:
        where.append("status = ?")
        args.append(status)
    if category:
        where.append("category = ?")
        args.append(category)
    if query:
        digits = re.sub(r"\D", "", query)
        if digits:
            where.append("(phone LIKE ? ESCAPE '\\' OR raw LIKE ? ESCAPE '\\')")
            args += [_like(digits), _like(query)]
        else:
            where.append("raw LIKE ? ESCAPE '\\'")
            args.append(_like(query))

    clause = (" WHERE " + " AND ".join(where)) if where else ""
    order = _COLLECTOR_SORTS.get(sort, _COLLECTOR_SORTS["reports"])
    limit = max(1, min(int(limit), 500))
    offset = max(0, int(offset))

    with _conn() as c:
        total = c.execute(
            f"SELECT COUNT(*) n FROM collector_numbers{clause}", args).fetchone()["n"]
        rows = c.execute(
            "SELECT phone, raw, category, status, reports, first_reported, "
            f"last_reported FROM collector_numbers{clause} "
            f"ORDER BY {order} LIMIT ? OFFSET ?",
            (*args, limit, offset),
        ).fetchall()
    return {"items": [dict(r) for r in rows], "total": int(total),
            "limit": limit, "offset": offset}


def delete_collector(phone: str) -> None:
    p = norm_phone(phone)
    with _conn() as c:
        c.execute("DELETE FROM collector_numbers WHERE phone = ?", (p,))
        c.execute("DELETE FROM collector_reports WHERE phone = ?", (p,))


def delete_whitelist(phone: str) -> None:
    with _conn() as c:
        c.execute("DELETE FROM collector_whitelist WHERE phone = ?", (norm_phone(phone),))


def log_call(user: str, from_phone: str, kind: str, at_iso: Optional[str] = None) -> None:
    with _conn() as c:
        c.execute("INSERT INTO call_events (user, from_phone, kind, at) VALUES (?, ?, ?, ?)",
                  (user, norm_phone(from_phone), kind, at_iso or _now_iso()))


def get_call_events(user: str, from_phone: str) -> list:
    p = norm_phone(from_phone)
    with _conn() as c:
        rows = c.execute(
            "SELECT kind, at FROM call_events WHERE user = ? AND from_phone = ? ORDER BY at",
            (user, p)).fetchall()
        return [dict(r) for r in rows]


# ---- Загруженные документы ----
def add_upload(email: str, doc_id: str, filename: str, size: int,
               mime: str, path: str) -> int:
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO document_uploads "
            "(email, doc_id, filename, size, mime, path, uploaded_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (email.lower(), doc_id, filename, size, mime, path, _now_iso()),
        )
        return int(cur.lastrowid or 0)


def list_uploads(email: str, doc_id: Optional[str] = None) -> list[dict]:
    sql = "SELECT id, doc_id, filename, size, mime, uploaded_at FROM document_uploads WHERE email = ?"
    args: tuple = (email.lower(),)
    if doc_id:
        sql += " AND doc_id = ?"
        args = (email.lower(), doc_id)
    sql += " ORDER BY id"
    with _conn() as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]


def get_upload(email: str, upload_id: int) -> Optional[dict]:
    with _conn() as c:
        r = c.execute(
            "SELECT id, doc_id, filename, size, mime, path, uploaded_at "
            "FROM document_uploads WHERE email = ? AND id = ?",
            (email.lower(), upload_id),
        ).fetchone()
        return dict(r) if r else None


def delete_upload(email: str, upload_id: int) -> bool:
    with _conn() as c:
        r = c.execute("SELECT path FROM document_uploads WHERE email = ? AND id = ?",
                      (email.lower(), upload_id)).fetchone()
        if not r:
            return False
        c.execute("DELETE FROM document_uploads WHERE email = ? AND id = ?",
                  (email.lower(), upload_id))
    return True


def uploaded_doc_ids(email: str) -> set:
    """Быстрая проверка — по каким doc_id есть хоть один файл."""
    with _conn() as c:
        rows = c.execute(
            "SELECT DISTINCT doc_id FROM document_uploads WHERE email = ?",
            (email.lower(),),
        ).fetchall()
        return {r["doc_id"] for r in rows}


# ---- Чат (Open Channels-кэш) ----
def add_chat_message(
    email: str, text: str, incoming: bool, *,
    is_file: bool = False, file_url: str = "", file_name: str = "",
    author_name: str = "", bitrix_msg_id: str = "",
) -> Optional[int]:
    """Сохраняет сообщение в локальный кэш чата.
    Возвращает id новой записи или None, если запись с таким bitrix_msg_id
    уже есть (дедупликация webhook'ов)."""
    try:
        with _conn() as c:
            cur = c.execute(
                "INSERT INTO chat_messages "
                "(email, text, incoming, is_file, file_url, file_name, "
                " author_name, bitrix_msg_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (email.lower(), text, 1 if incoming else 0,
                 1 if is_file else 0, file_url, file_name,
                 author_name, bitrix_msg_id, _now_iso()),
            )
            return int(cur.lastrowid or 0)
    except sqlite3.IntegrityError:
        # Уже есть такой bitrix_msg_id — вебхук пришёл повторно.
        return None


def list_chat_messages(email: str, limit: int = 200) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT id, text, incoming, is_file, file_url, file_name, "
            "author_name, created_at "
            "FROM chat_messages WHERE email = ? ORDER BY id ASC LIMIT ?",
            (email.lower(), limit),
        ).fetchall()
        return [dict(r) for r in rows]


def unread_chat_count(email: str) -> int:
    """Число непрочитанных входящих (от юриста) сообщений клиента."""
    with _conn() as c:
        r = c.execute(
            "SELECT COUNT(*) n FROM chat_messages "
            "WHERE email = ? AND incoming = 1 AND read = 0",
            (email.lower(),),
        ).fetchone()
        return int(r["n"] if r else 0)


def mark_chat_read(email: str) -> int:
    """Пометить все входящие сообщения клиента как прочитанные.
    Возвращает число обновлённых строк."""
    with _conn() as c:
        cur = c.execute(
            "UPDATE chat_messages SET read = 1 "
            "WHERE email = ? AND incoming = 1 AND read = 0",
            (email.lower(),),
        )
        return cur.rowcount or 0


# ---- Bitrix OAuth (Local App) ----
def save_bitrix_oauth(
    *, domain: str, member_id: str,
    access_token: str, refresh_token: str,
    expires_in: int, scope: str = "",
) -> None:
    """Сохранить/обновить токены Битрикс. TTL из Битрикса ≈ 3600 сек."""
    now = _now_iso()
    expires_at = int(time.time()) + int(expires_in)
    with _conn() as c:
        c.execute(
            "INSERT INTO bitrix_oauth "
            "(id, domain, member_id, access_token, refresh_token, "
            " expires_at, scope, installed_at, updated_at) "
            "VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET "
            " domain = excluded.domain, "
            " member_id = excluded.member_id, "
            " access_token = excluded.access_token, "
            " refresh_token = excluded.refresh_token, "
            " expires_at = excluded.expires_at, "
            " scope = excluded.scope, "
            " updated_at = excluded.updated_at",
            (domain, member_id, access_token, refresh_token,
             expires_at, scope, now, now),
        )


def get_bitrix_oauth() -> Optional[dict]:
    with _conn() as c:
        r = c.execute("SELECT * FROM bitrix_oauth WHERE id = 1").fetchone()
        return dict(r) if r else None


def pm_get_month_event(email: str, year_month: str) -> Optional[dict]:
    """Есть ли у клиента событие «получил» или «запросил» за конкретный месяц?
    Возвращает последнее событие месяца или None."""
    with _conn() as c:
        r = c.execute(
            "SELECT id, kind, method, file_url, bitrix_task_id, created_at "
            "FROM pm_events WHERE email = ? AND year_month = ? "
            "ORDER BY id DESC LIMIT 1",
            (email.lower(), year_month),
        ).fetchone()
        return dict(r) if r else None


def pm_mark_received(email: str, year_month: str) -> int:
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO pm_events (email, year_month, kind, created_at) "
            "VALUES (?, ?, 'received', ?)",
            (email.lower(), year_month, _now_iso()),
        )
        return int(cur.lastrowid or 0)


def pm_delete_month_event(email: str, year_month: str, kind: str) -> int:
    """Удалить все события клиента за месяц с указанным kind.
    Возвращает число удалённых строк."""
    with _conn() as c:
        cur = c.execute(
            "DELETE FROM pm_events WHERE email = ? AND year_month = ? AND kind = ?",
            (email.lower(), year_month, kind),
        )
        return int(cur.rowcount or 0)


def pm_mark_consent_requested(
    email: str, year_month: str, method: str,
    file_url: str, bitrix_task_id: int,
) -> int:
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO pm_events (email, year_month, kind, method, file_url, "
            "bitrix_task_id, created_at) VALUES (?, ?, 'consent_requested', "
            "?, ?, ?, ?)",
            (email.lower(), year_month, method, file_url, bitrix_task_id,
             _now_iso()),
        )
        return int(cur.lastrowid or 0)


def pm_escalation_get(email: str, year_month: str) -> Optional[dict]:
    with _conn() as c:
        r = c.execute(
            "SELECT * FROM pm_escalations WHERE email = ? AND year_month = ?",
            (email.lower(), year_month),
        ).fetchone()
        return dict(r) if r else None


def pm_escalation_create(
    email: str, year_month: str, month_end: str, task_id: int,
) -> None:
    """Клиент нажал «не получил» — стартуем уровень 1. Если запись
    уже есть — идемпотентно не пересоздаём (клиент повторно нажал)."""
    now = _now_iso()
    with _conn() as c:
        c.execute(
            "INSERT OR IGNORE INTO pm_escalations "
            "(email, year_month, level, started_at, month_end, "
            " level_reached_at, task_ids) "
            "VALUES (?, ?, 1, ?, ?, ?, ?)",
            (email.lower(), year_month, now, month_end, now, str(task_id or "")),
        )


def pm_escalation_bump_level(
    email: str, year_month: str, new_level: int, task_ids_csv: str,
) -> None:
    now = _now_iso()
    with _conn() as c:
        c.execute(
            "UPDATE pm_escalations SET level = ?, level_reached_at = ?, "
            "task_ids = COALESCE(task_ids, '') || CASE WHEN task_ids = '' "
            "THEN ? ELSE ',' || ? END WHERE email = ? AND year_month = ?",
            (new_level, now, task_ids_csv, task_ids_csv,
             email.lower(), year_month),
        )


def pm_escalation_resolve(email: str, year_month: str) -> bool:
    """Клиент отметил «получил» — закрываем эскалацию. Возвращает True
    если была не-закрытая запись (значит клиент реально «отсидел» задержку)."""
    with _conn() as c:
        r = c.execute(
            "SELECT resolved_at, level FROM pm_escalations "
            "WHERE email = ? AND year_month = ?",
            (email.lower(), year_month),
        ).fetchone()
        if not r or r["resolved_at"]:
            return False
        c.execute(
            "UPDATE pm_escalations SET resolved_at = ? "
            "WHERE email = ? AND year_month = ?",
            (_now_iso(), email.lower(), year_month),
        )
        return True


def pm_escalations_active() -> List[dict]:
    """Все незакрытые эскалации — для крона."""
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM pm_escalations WHERE resolved_at IS NULL"
        ).fetchall()
        return [dict(r) for r in rows]


def make_ref_code(email: str) -> str:
    """Стабильный ref-код из email клиента. Всегда 8 символов.

    Реализация: sha1(email.lower())[:8].upper() — от одного email всегда
    получим тот же код, ничего хранить не надо.
    """
    import hashlib
    h = hashlib.sha1(email.lower().encode()).hexdigest()
    return h[:8].upper()


def save_referral(
    *, ref_code: str, referrer_email: str, friend_name: str,
    friend_phone: str, friend_email: str, debt_amount: str,
    comment: str, bitrix_lead_id: int,
) -> int:
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO referrals (ref_code, referrer_email, friend_name, "
            "friend_phone, friend_email, debt_amount, comment, bitrix_lead_id, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (ref_code, referrer_email.lower(), friend_name, friend_phone,
             friend_email.lower(), debt_amount, comment, bitrix_lead_id,
             _now_iso()),
        )
        return int(cur.lastrowid or 0)


def find_referrer_by_code(ref_code: str) -> Optional[str]:
    """По ref-коду найти email клиента, который его выпустил.

    Поскольку ref-код детерминирован от email (см. make_ref_code), мы
    просто ищем в БД любого клиента, у которого этот код совпадает.
    """
    ref_code = ref_code.upper()
    with _conn() as c:
        rows = c.execute("SELECT email FROM users").fetchall()
        for r in rows:
            if make_ref_code(r["email"]) == ref_code:
                return r["email"]
    return None


def list_referrals_by_referrer(email: str) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT id, friend_name, status, payout_amount, created_at "
            "FROM referrals WHERE referrer_email = ? ORDER BY id DESC",
            (email.lower(),),
        ).fetchall()
        return [dict(r) for r in rows]


def referrer_stats(email: str) -> dict:
    """Статистика для клиента: сколько привёл, сколько заработал."""
    with _conn() as c:
        rows = c.execute(
            "SELECT COUNT(*) n, "
            "SUM(CASE WHEN status = 'converted' THEN 1 ELSE 0 END) c, "
            "SUM(payout_amount) s "
            "FROM referrals WHERE referrer_email = ?",
            (email.lower(),),
        ).fetchone()
        # Лучший результат месяца по всей базе (кол-во заявок).
        best = c.execute(
            "SELECT referrer_email, COUNT(*) n FROM referrals "
            "WHERE substr(created_at, 1, 7) = substr(?, 1, 7) "
            "GROUP BY referrer_email ORDER BY n DESC LIMIT 1",
            (_now_iso(),),
        ).fetchone()
        return {
            "total": int(rows["n"] or 0),
            "converted": int(rows["c"] or 0),
            "earned": int(rows["s"] or 0),
            "best_this_month": int(best["n"]) if best else 0,
        }


def list_all_referrals(limit: int = 200) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT id, ref_code, referrer_email, friend_name, friend_phone, "
            "friend_email, debt_amount, comment, bitrix_lead_id, status, "
            "payout_amount, created_at FROM referrals ORDER BY id DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
        return [dict(r) for r in rows]


def save_nps_score(email: str, score: int) -> int:
    """Сохранить NPS-оценку. Возвращает id записи. Ожидается score в 8..10."""
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO nps_scores (email, score, created_at) VALUES (?, ?, ?)",
            (email.lower(), int(score), _now_iso()),
        )
        return int(cur.lastrowid or 0)


def mark_nps_review_left(nps_id: int) -> None:
    """Клиент нажал «Оставить отзыв на Яндекс Картах»."""
    with _conn() as c:
        c.execute("UPDATE nps_scores SET left_review = 1 WHERE id = ?", (nps_id,))


def list_nps_scores(limit: int = 200) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT id, email, score, left_review, created_at "
            "FROM nps_scores ORDER BY id DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
        return [dict(r) for r in rows]


def payment_reminder_was_sent(email: str, due_date: str, kind: str = "3days") -> bool:
    """Проверяем — уже отправляли клиенту напоминание об этой дате?"""
    with _conn() as c:
        r = c.execute(
            "SELECT 1 FROM payment_reminders "
            "WHERE email = ? AND due_date = ? AND kind = ? LIMIT 1",
            (email.lower(), due_date, kind),
        ).fetchone()
    return r is not None


def payment_reminder_mark_sent(email: str, due_date: str, amount: int,
                                kind: str = "3days") -> None:
    """Записать факт отправки — блокирует повторную отправку тому же клиенту
    для того же платежа."""
    with _conn() as c:
        c.execute(
            "INSERT OR IGNORE INTO payment_reminders "
            "(email, due_date, kind, amount, sent_at) VALUES (?,?,?,?,?)",
            (email.lower(), due_date, kind, int(amount), _now_iso()),
        )


def list_active_users(limit: int = 5000) -> list[dict]:
    """Все клиенты которые хоть раз залогинились в приложении — по ним и
    крутим ежедневный чек графика платежей. blocked=1 пропускаем."""
    with _conn() as c:
        rows = c.execute(
            "SELECT email, name, phone FROM users "
            "WHERE COALESCE(blocked,0) = 0 "
            "ORDER BY email LIMIT ?",
            (int(limit),),
        ).fetchall()
        return [dict(r) for r in rows]


# ---- Сводка и тренды ----
def nps_trend(months: int = 6) -> list[dict]:
    """Помесячный тренд оценок за последние N месяцев.

    ВАЖНО: в nps_scores лежат только оценки 8-10 (промоутеры) — так решено
    бизнесом. Классический NPS (%промоутеров − %детракторов) из этих данных
    не считается, поэтому возвращаем среднюю оценку промоутеров и их число.
    Месяцы без оценок отдаём нулями, иначе график врёт по оси времени.
    """
    months = max(1, min(int(months), 24))
    with _conn() as c:
        rows = c.execute(
            "SELECT substr(created_at, 1, 7) ym, COUNT(*) n, "
            "AVG(score) avg_score, SUM(left_review) reviews "
            "FROM nps_scores GROUP BY ym"
        ).fetchall()
    by_month = {r["ym"]: r for r in rows}

    # Ключи последних N месяцев, от старого к новому.
    today = datetime.now(timezone.utc)
    keys: list[str] = []
    y, m = today.year, today.month
    for _ in range(months):
        keys.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    keys.reverse()

    out = []
    for k in keys:
        r = by_month.get(k)
        out.append({
            "month": k,
            "count": int(r["n"]) if r else 0,
            "avg_score": round(float(r["avg_score"]), 2) if r else 0.0,
            "reviews": int(r["reviews"] or 0) if r else 0,
        })
    return out


def stats() -> dict:
    """KPI-сводка для шапки админки: пользователи, коллекторы, NPS."""
    now = datetime.now(timezone.utc)
    week_ago = (now - timedelta(days=7)).timestamp()
    month_ago = (now - timedelta(days=30)).timestamp()

    def _ts(value) -> Optional[float]:
        """ISO-строка → unix. None, если формат неожиданный (старые записи)."""
        try:
            return datetime.fromisoformat(value).timestamp()
        except (TypeError, ValueError):
            return None

    with _conn() as c:
        total = c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
        blocked = c.execute(
            "SELECT COUNT(*) n FROM users WHERE blocked = 1").fetchone()["n"]

        # Активность и приток считаем одним проходом по датам.
        active_7d = active_30d = new_7d = 0
        for r in c.execute("SELECT last_seen, first_seen FROM users").fetchall():
            seen = _ts(r["last_seen"])
            if seen is not None:
                if seen >= week_ago:
                    active_7d += 1
                if seen >= month_ago:
                    active_30d += 1
            first = _ts(r["first_seen"])
            if first is not None and first >= week_ago:
                new_7d += 1

        logins = c.execute(
            "SELECT COUNT(*) n FROM events WHERE kind = 'login'").fetchone()["n"]

        col = c.execute(
            "SELECT COUNT(*) total, "
            "SUM(CASE WHEN status = 'confirmed' THEN 1 ELSE 0 END) confirmed, "
            "SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) pending "
            "FROM collector_numbers").fetchone()
        whitelist = c.execute(
            "SELECT COUNT(*) n FROM collector_whitelist").fetchone()["n"]

        nps = c.execute(
            "SELECT COUNT(*) n, AVG(score) avg_score, SUM(left_review) reviews "
            "FROM nps_scores").fetchone()
        nps_month = c.execute(
            "SELECT COUNT(*) n, AVG(score) avg_score FROM nps_scores "
            "WHERE created_at >= ?",
            ((now - timedelta(days=30)).isoformat(timespec="seconds"),),
        ).fetchone()

    nps_count = int(nps["n"] or 0)
    reviews = int(nps["reviews"] or 0)
    return {
        # Ключи total/blocked/active_7d/logins сохранены — на них опирается
        # существующий UI и возможные внешние вызовы.
        "total": total,
        "blocked": blocked,
        "active_7d": active_7d,
        "logins": logins,
        "active_30d": active_30d,
        "new_7d": new_7d,
        "collectors": {
            "total": int(col["total"] or 0),
            "confirmed": int(col["confirmed"] or 0),
            "pending": int(col["pending"] or 0),
            "whitelist": int(whitelist),
        },
        "nps": {
            "count": nps_count,
            "avg_score": round(float(nps["avg_score"]), 2) if nps_count else 0.0,
            "reviews": reviews,
            "review_rate": round(reviews * 100 / nps_count, 1) if nps_count else 0.0,
            "count_30d": int(nps_month["n"] or 0),
            "avg_score_30d": (round(float(nps_month["avg_score"]), 2)
                              if nps_month["n"] else 0.0),
        },
    }
