"""Жалобы клиентов: приём, ограничения, разбор отделом контроля качества.

Политика взята из исследования — `docs/research-complaints.md`. Коротко:

Главное ограничение — не счётчик, а дедупликация: пока по категории есть
открытая жалоба, вторую по той же теме подать нельзя. Так устроено
досудебное обжалование на Госуслугах, и это работает лучше лимита, потому
что снимает саму причину дублей: человек шлёт второе обращение, когда не
понимает, что с первым. Клиенту показываем не «лимит исчерпан», а срок
ответа по уже поданной.

Числовые лимиты сверху — грубый предохранитель от скрипта, не от человека.

Срок ответа 10 дней — требование Закона о защите прав потребителей: мы
оказываем юридические услуги физлицам, и просрочка даёт право на неустойку.

Таблица собственная, как и в `quality.py`: общую схему не трогаем.
"""
import logging
import sqlite3
from datetime import datetime, timedelta, timezone

from . import bitrix
from .config import settings

log = logging.getLogger(__name__)

# Категории. Фиксированы в коде, а не в настройках: от них зависит правило
# «одна открытая на категорию», и произвольный список из .env сделал бы
# поведение непредсказуемым.
CATEGORIES = {
    "manager":   "Работа менеджера",
    "deadlines": "Сроки по делу",
    "money":     "Деньги и платежи",
    "app":       "Мобильное приложение",
    "other":     "Другое",
}

STATUSES = {
    "open":        "Новая",
    "in_progress": "В работе",
    "resolved":    "Решена",
    "rejected":    "Отклонена",
}
# Пока жалоба в этих статусах, повторную по той же категории не принимаем.
OPEN_STATUSES = ("open", "in_progress")


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(settings.db_path)
    c.row_factory = sqlite3.Row
    return c


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _add_workdays(start: datetime, days: int) -> datetime:
    """Прибавить рабочие дни. Праздники не учитываем — производственный
    календарь тут избыточен, дедлайн и так взят с запасом к сроку ответа."""
    d = start
    while days > 0:
        d += timedelta(days=1)
        if d.weekday() < 5:
            days -= 1
    return d


def init() -> None:
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS complaints (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL,
                category TEXT NOT NULL,        -- см. CATEGORIES
                text TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                qc_task_id INTEGER DEFAULT 0,  -- задача контроля качества
                attempts INTEGER DEFAULT 0,
                last_error TEXT DEFAULT '',
                manager_id INTEGER DEFAULT 0,  -- ответственный из Битрикса
                manager_name TEXT DEFAULT '',
                answer_due TEXT NOT NULL,      -- до какой даты обязаны ответить
                resolution TEXT DEFAULT '',    -- что ответили
                resolved_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_complaints_email "
                  "ON complaints (email, status)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_complaints_status "
                  "ON complaints (status, created_at)")


# ---------------------------------------------------------------------------
# Правила подачи
# ---------------------------------------------------------------------------
class Rejected(Exception):
    """Жалоба не принята. Текст предназначен клиенту — он увидит его
    в приложении, поэтому пишется по-человечески и по существу."""

    def __init__(self, message: str, reason: str):
        super().__init__(message)
        self.message = message
        self.reason = reason  # машинный код для аналитики и тестов


def _fmt_date(iso: str) -> str:
    """ISO → «12 июля». Клиенту незачем видеть машинный формат."""
    MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля",
              "августа", "сентября", "октября", "ноября", "декабря")
    try:
        d = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return iso or ""
    return f"{d.day} {MONTHS[d.month - 1]}"


def check_can_submit(email: str, category: str) -> None:
    """Бросает Rejected, если подавать нельзя. Молча возвращается, если можно."""
    email = (email or "").lower().strip()
    if category not in CATEGORIES:
        raise Rejected("Выберите тему жалобы.", "bad_category")

    now = _now()
    with _conn() as c:
        # 1. Дедупликация по теме — основное правило.
        same = c.execute(
            "SELECT created_at, answer_due FROM complaints "
            f"WHERE email = ? AND category = ? AND status IN "
            f"({','.join('?' * len(OPEN_STATUSES))}) "
            "ORDER BY id DESC LIMIT 1",
            (email, category, *OPEN_STATUSES),
        ).fetchone()
        if same:
            raise Rejected(
                f"Ваша жалоба по этой теме от {_fmt_date(same['created_at'])} "
                f"ещё в работе. Мы ответим до {_fmt_date(same['answer_due'])}. "
                "Если хотите добавить детали — дождитесь ответа или напишите "
                "в чат с юристом.",
                "duplicate_category")

        # 2. Пауза между подачами.
        last = c.execute(
            "SELECT created_at FROM complaints WHERE email = ? "
            "ORDER BY id DESC LIMIT 1", (email,)).fetchone()
        if last:
            try:
                elapsed = (now - datetime.fromisoformat(last["created_at"])).total_seconds()
            except (TypeError, ValueError):
                elapsed = settings.complaint_cooldown_seconds
            if elapsed < settings.complaint_cooldown_seconds:
                wait = int((settings.complaint_cooldown_seconds - elapsed) / 60) + 1
                raise Rejected(
                    f"Вы только что отправили жалобу. Следующую можно подать "
                    f"через {wait} мин.", "cooldown")

        # 3. Предохранители от потока.
        day = c.execute(
            "SELECT COUNT(*) n FROM complaints WHERE email = ? AND created_at >= ?",
            (email, _iso(now - timedelta(days=1)))).fetchone()["n"]
        if day >= settings.complaint_max_per_day:
            raise Rejected(
                "За сутки можно подать не больше "
                f"{settings.complaint_max_per_day} жалоб. Попробуйте завтра.",
                "day_limit")

        month = c.execute(
            "SELECT COUNT(*) n FROM complaints WHERE email = ? AND created_at >= ?",
            (email, _iso(now - timedelta(days=30)))).fetchone()["n"]
        if month >= settings.complaint_max_per_month:
            raise Rejected(
                "За месяц можно подать не больше "
                f"{settings.complaint_max_per_month} жалоб. Если проблема не "
                "решается — напишите в чат с юристом.", "month_limit")


def submit(email: str, category: str, text: str) -> dict:
    """Принять жалобу. Возвращает {id, answer_due}.

    Задачу в Битриксе здесь НЕ создаём: поход в Битрикс занимает секунды,
    и клиент в приложении ждал бы их, глядя на крутилку. Задачу поставит
    воркер на ближайшем прогоне — при сроке ответа в 10 дней десять минут
    задержки ничего не меняют.
    """
    email = (email or "").lower().strip()
    text = (text or "").strip()
    if not email:
        raise Rejected("Не удалось определить, от кого жалоба.", "no_email")
    if len(text) < settings.complaint_text_min:
        raise Rejected(
            "Опишите проблему подробнее — минимум "
            f"{settings.complaint_text_min} символов.", "text_too_short")
    if len(text) > settings.complaint_text_max:
        raise Rejected(
            f"Слишком длинный текст, максимум {settings.complaint_text_max} "
            "символов.", "text_too_long")

    check_can_submit(email, category)

    now = _now()
    due = now + timedelta(days=settings.complaint_answer_days)
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO complaints (email, category, text, status, answer_due, "
            " created_at, updated_at) VALUES (?, ?, ?, 'open', ?, ?, ?)",
            (email, category, text, _iso(due), _iso(now), _iso(now)),
        )
        cid = int(cur.lastrowid or 0)
    log.info("Жалоба №%s от %s, тема «%s»", cid, email, CATEGORIES[category])
    return {"id": cid, "answer_due": _iso(due),
            "answer_days": settings.complaint_answer_days}


# ---------------------------------------------------------------------------
# Разбор воркером: менеджер и задача
# ---------------------------------------------------------------------------
def _pending() -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM complaints WHERE qc_task_id = 0 AND attempts < ? "
            f"AND status IN ({','.join('?' * len(OPEN_STATUSES))}) "
            "ORDER BY id",
            (settings.qc_task_max_attempts, *OPEN_STATUSES),
        ).fetchall()
        return [dict(r) for r in rows]


def _task_text(row: dict) -> tuple[str, str]:
    cat = CATEGORIES.get(row["category"], row["category"])
    manager = row["manager_name"] or (f"ID {row['manager_id']}"
                                      if row["manager_id"] else "не определён")
    title = f"Жалоба клиента: {cat} — {row['email']}"
    body = (
        f"[B]Жалоба №{row['id']}[/B]\n\n"
        f"Клиент: {row['email']}\n"
        f"Тема: {cat}\n"
        f"Подана: {(row['created_at'] or '').replace('T', ' ')[:16]}\n"
        f"Ответственный менеджер: {manager}\n"
        f"[B]Ответить клиенту до: {(row['answer_due'] or '')[:10]}[/B] "
        f"(срок по Закону о защите прав потребителей)\n\n"
        f"[B]Текст жалобы[/B]\n{row['text']}\n\n"
        "После разбора отметьте результат в админ-панели, раздел «Жалобы»."
    )
    return title, body


def process_pending() -> dict:
    """Привязать жалобы к менеджеру и поставить задачи контроля качества."""
    due = _pending()
    if not due:
        return {"created": 0, "failed": 0, "due": 0}

    if not bitrix.configured() or not settings.bitrix_qc_head_id:
        log.warning("Жалоб к разбору: %d, но задачи не создаются "
                    "(нет BITRIX_WEBHOOK_URL или BITRIX_QC_HEAD_ID)", len(due))
        return {"created": 0, "failed": 0, "due": len(due)}

    deadline = _iso(_add_workdays(_now(),
                                  settings.complaint_task_deadline_workdays))
    created = failed = 0

    for row in due:
        # Менеджер — вспомогательная информация. Если Битрикс не отдал, задачу
        # всё равно ставим: жалоба важнее подписи в её тексте.
        if not row["manager_id"]:
            try:
                mid, mname = bitrix.resolve_manager(row["email"])
                if mid:
                    row["manager_id"], row["manager_name"] = mid, mname
                    with _conn() as c:
                        c.execute(
                            "UPDATE complaints SET manager_id = ?, "
                            "manager_name = ?, updated_at = ? WHERE id = ?",
                            (mid, mname, _iso(_now()), row["id"]))
            except bitrix.BitrixError as e:
                log.warning("Менеджер по жалобе %s не определён: %s", row["id"], e)

        title, body = _task_text(row)
        try:
            task_id = bitrix.create_task(title, body,
                                         settings.bitrix_qc_head_id, deadline)
            with _conn() as c:
                c.execute(
                    "UPDATE complaints SET qc_task_id = ?, last_error = '', "
                    "updated_at = ? WHERE id = ?",
                    (task_id, _iso(_now()), row["id"]))
            created += 1
        except bitrix.BitrixError as e:
            failed += 1
            with _conn() as c:
                c.execute(
                    "UPDATE complaints SET attempts = attempts + 1, "
                    "last_error = ?, updated_at = ? WHERE id = ?",
                    (str(e)[:300], _iso(_now()), row["id"]))
            log.warning("Задача по жалобе %s не создана: %s", row["id"], e)
            if "сеть недоступна" in str(e):
                break

    return {"created": created, "failed": failed, "due": len(due)}


# ---------------------------------------------------------------------------
# Для админки
# ---------------------------------------------------------------------------
def _like(q: str) -> str:
    q = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{q}%"


def list_complaints(status: str = "", category: str = "", query: str = "",
                    overdue_only: bool = False,
                    limit: int = 50, offset: int = 0) -> dict:
    where, args = [], []
    if status:
        where.append("status = ?")
        args.append(status)
    if category:
        where.append("category = ?")
        args.append(category)
    if query:
        where.append("(email LIKE ? ESCAPE '\\' OR text LIKE ? ESCAPE '\\')")
        args += [_like(query), _like(query)]
    if overdue_only:
        where.append(f"answer_due < ? AND status IN "
                     f"({','.join('?' * len(OPEN_STATUSES))})")
        args += [_iso(_now()), *OPEN_STATUSES]

    clause = (" WHERE " + " AND ".join(where)) if where else ""
    limit = max(1, min(int(limit), 500))
    offset = max(0, int(offset))

    with _conn() as c:
        total = c.execute(
            f"SELECT COUNT(*) n FROM complaints{clause}", args).fetchone()["n"]
        rows = c.execute(
            f"SELECT * FROM complaints{clause} ORDER BY id DESC LIMIT ? OFFSET ?",
            (*args, limit, offset)).fetchall()

    now_iso = _iso(_now())
    items = []
    for r in rows:
        d = dict(r)
        d["category_label"] = CATEGORIES.get(d["category"], d["category"])
        d["status_label"] = STATUSES.get(d["status"], d["status"])
        d["overdue"] = bool(d["status"] in OPEN_STATUSES
                            and (d["answer_due"] or "") < now_iso)
        items.append(d)
    return {"items": items, "total": int(total), "limit": limit,
            "offset": offset, "categories": CATEGORIES, "statuses": STATUSES}


def set_status(complaint_id: int, status: str, resolution: str = "") -> dict:
    if status not in STATUSES:
        raise ValueError("Неизвестный статус")
    now = _iso(_now())
    closed = status in ("resolved", "rejected")
    with _conn() as c:
        cur = c.execute(
            "UPDATE complaints SET status = ?, resolution = ?, "
            "resolved_at = CASE WHEN ? THEN ? ELSE NULL END, updated_at = ? "
            "WHERE id = ?",
            (status, resolution[:2000], 1 if closed else 0, now, now,
             int(complaint_id)),
        )
        if not cur.rowcount:
            raise LookupError("Жалоба не найдена")
    return {"ok": True, "id": int(complaint_id), "status": status}


def stats() -> dict:
    """Сводка для дашборда и месячного отчёта."""
    now = _now()
    with _conn() as c:
        row = c.execute(
            "SELECT COUNT(*) total, "
            f"SUM(CASE WHEN status IN ({','.join('?' * len(OPEN_STATUSES))}) "
            "THEN 1 ELSE 0 END) open, "
            "SUM(CASE WHEN created_at >= ? THEN 1 ELSE 0 END) last_30d "
            "FROM complaints",
            (*OPEN_STATUSES, _iso(now - timedelta(days=30)))).fetchone()
        overdue = c.execute(
            "SELECT COUNT(*) n FROM complaints WHERE answer_due < ? "
            f"AND status IN ({','.join('?' * len(OPEN_STATUSES))})",
            (_iso(now), *OPEN_STATUSES)).fetchone()["n"]
    return {"total": int(row["total"] or 0), "open": int(row["open"] or 0),
            "last_30d": int(row["last_30d"] or 0), "overdue": int(overdue)}


def month_summary(year_month: str) -> list[dict]:
    """Жалобы месяца — для отчёта руководителям."""
    with _conn() as c:
        rows = c.execute(
            "SELECT id, email, category, status, created_at, qc_task_id "
            "FROM complaints WHERE substr(created_at, 1, 7) = ? ORDER BY id",
            (year_month,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["category_label"] = CATEGORIES.get(d["category"], d["category"])
        d["status_label"] = STATUSES.get(d["status"], d["status"])
        out.append(d)
    return out
