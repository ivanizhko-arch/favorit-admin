"""Контроль качества по оценкам клиентов и рейтинг менеджеров.

Два независимых потока:

1. Оценка 0-6 — недовольный клиент. Разбирается в течение минут: воркер
   ставит задачу на отдел контроля качества. Ждать конца месяца тут нельзя.
2. Оценки 8-10 копятся и раз в месяц превращаются в рейтинг менеджеров.
   Отчёт уходит задачами руководителям контроля качества, сопровождения
   и генеральному директору.

Ровно 7 — нейтральная оценка: ни в контроль, ни в рейтинг.

Таблицы здесь **собственные**. Общую `nps_scores` не трогаем: её схема
делится с главным сервисом, а всё, что мы про оценку узнали (кто вёл
клиента) и сделали (какую задачу поставили), лежит рядом, в `nps_followup`.
Поэтому и подключение своё, и `init()` отдельный от `db.init()`.
"""
import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional

from . import bitrix, store
from .config import settings

log = logging.getLogger(__name__)


def _conn() -> sqlite3.Connection:
    return store.connect()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def init() -> None:
    """Схема, принадлежащая только админ-сервису. Главный сервис эти таблицы
    не создаёт, не читает и не пишет."""
    with _conn() as c:
        # Что мы узнали и сделали по каждой оценке. nps_id ссылается на
        # nps_scores.id, но без FOREIGN KEY: главный сервис про эту таблицу
        # не знает, и связь на уровне БД мешала бы ему удалять записи.
        c.execute("""
            CREATE TABLE IF NOT EXISTS nps_followup (
                nps_id INTEGER PRIMARY KEY,
                email TEXT NOT NULL,
                score INTEGER NOT NULL,
                score_created_at TEXT NOT NULL,  -- когда клиент поставил оценку
                manager_id INTEGER DEFAULT 0,    -- ответственный в Битриксе
                manager_name TEXT DEFAULT '',
                qc_task_id INTEGER DEFAULT 0,    -- задача контроля качества (для 0-6)
                attempts INTEGER DEFAULT 0,      -- попыток создать задачу
                last_error TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_followup_month "
                  "ON nps_followup (score_created_at)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_followup_manager "
                  "ON nps_followup (manager_id)")
        # Факт отправки месячного отчёта. Ключ по месяцу — рестарт сервиса
        # или повторный запуск воркера не отправит его второй раз.
        c.execute("""
            CREATE TABLE IF NOT EXISTS nps_monthly_report (
                year_month TEXT PRIMARY KEY,     -- '2026-07'
                sent_at TEXT NOT NULL,
                task_ids TEXT DEFAULT '',        -- csv id задач в Битриксе
                leader_manager_id INTEGER DEFAULT 0,
                leader_name TEXT DEFAULT '',
                leader_net INTEGER DEFAULT 0,    -- лучшие минус низкие у лидера
                snapshot TEXT DEFAULT ''         -- JSON рейтинга на момент отправки
            )
        """)


# ---------------------------------------------------------------------------
# Градация
# ---------------------------------------------------------------------------
def category(score: int) -> str:
    """low — в контроль качества, top — в рейтинг, neutral — никуда.

    invalid — оценка вне шкалы 0-10. Основной сервис диапазон не проверяет,
    и без этой ветки запись со значением 42 попадала бы в рейтинг менеджера
    как отличная, а отрицательная — порождала задачу контролю качества.
    """
    if score < 0 or score > 10:
        return "invalid"
    if score <= settings.qc_detractor_max:
        return "low"
    if score >= settings.qc_promoter_min:
        return "top"
    return "neutral"


def _range_label(lo: int, hi: int) -> str:
    """«8-10» или просто «8», если границы совпали. Иначе при узкой полосе
    нейтральных в отчёте появлялось бы «8-8»."""
    return str(lo) if lo >= hi else f"{lo}-{hi}"


def grades() -> dict:
    """Границы градации и готовые подписи. Интерфейс берёт их отсюда, чтобы
    не хранить «0-6» в разметке: один раз уже разъехалось при переносе
    семёрки в низкие."""
    low_max = settings.qc_detractor_max
    top_min = settings.qc_promoter_min
    return {
        "low_max": low_max,
        "promoter_min": top_min,
        "low_label": _range_label(0, low_max),
        "neutral_label": _range_label(low_max + 1, top_min - 1),
        "top_label": _range_label(top_min, 10),
        "has_neutral": low_max + 1 <= top_min - 1,
    }


def _parse(iso: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Разбор новых оценок
# ---------------------------------------------------------------------------
def _pending_scores(limit: int) -> list[dict]:
    """Оценки, которых мы ещё не касались."""
    with _conn() as c:
        rows = c.execute(
            "SELECT s.id, s.email, s.score, s.created_at "
            "FROM nps_scores s "
            "LEFT JOIN nps_followup f ON f.nps_id = s.id "
            "WHERE f.nps_id IS NULL "
            "ORDER BY s.id LIMIT ?",
            (int(limit),),
        ).fetchall()
        return [dict(r) for r in rows]


def process_new_scores(limit: Optional[int] = None) -> dict:
    """Привязать новые оценки к менеджерам.

    Ошибка сети прерывает пакет, а не пропускает оценку: строку без записи
    в nps_followup воркер возьмёт на следующем прогоне. Иначе оценка молча
    осталась бы без менеджера навсегда.
    """
    limit = limit or settings.qc_batch_size
    pending = _pending_scores(limit)
    linked = skipped = 0

    for s in pending:
        manager_id, manager_name = 0, ""
        if bitrix.configured():
            try:
                manager_id, manager_name = bitrix.resolve_manager(s["email"])
            except bitrix.BitrixError as e:
                log.warning("Битрикс недоступен, разбор отложен: %s", e)
                skipped = len(pending) - linked
                break
        now = _now_iso()
        with _conn() as c:
            c.execute(
                "INSERT OR IGNORE INTO nps_followup "
                "(nps_id, email, score, score_created_at, manager_id, "
                " manager_name, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (s["id"], s["email"], int(s["score"]), s["created_at"],
                 manager_id, manager_name, now, now),
            )
        linked += 1

    return {"linked": linked, "skipped": skipped, "pending_seen": len(pending)}


# ---------------------------------------------------------------------------
# Задачи по низким оценкам
# ---------------------------------------------------------------------------
def _tasks_due() -> list[dict]:
    cutoff = (datetime.now(timezone.utc)
              - timedelta(days=settings.qc_task_max_age_days)
              ).isoformat(timespec="seconds")
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM nps_followup "
            # score >= 0 отсекает записи вне шкалы: по оценке «-3» задача
            # контролю качества не нужна, это битые данные, а не недовольство.
            "WHERE score BETWEEN 0 AND ? AND qc_task_id = 0 AND attempts < ? "
            "AND score_created_at >= ? "
            "ORDER BY nps_id",
            (settings.qc_detractor_max, settings.qc_task_max_attempts, cutoff),
        ).fetchall()
        return [dict(r) for r in rows]


def _qc_task_text(row: dict) -> tuple[str, str]:
    when = (row["score_created_at"] or "").replace("T", " ")[:16]
    manager = row["manager_name"] or (f"ID {row['manager_id']}"
                                      if row["manager_id"] else "не определён")
    title = f"Низкая оценка {row['score']}/10 — {row['email']}"
    body = (
        f"Клиент поставил оценку [B]{row['score']} из 10[/B] в мобильном приложении.\n\n"
        f"Клиент: {row['email']}\n"
        f"Дата оценки: {when}\n"
        f"Ответственный менеджер: {manager}\n\n"
        "Просьба связаться с клиентом, выяснить причину и зафиксировать "
        "результат в комментарии к задаче."
    )
    return title, body


def create_pending_tasks() -> dict:
    """Поставить задачи контроля качества по неразобранным низким оценкам."""
    due = _tasks_due()
    created = failed = 0
    if not due:
        return {"created": 0, "failed": 0, "due": 0}

    if not bitrix.configured() or not settings.bitrix_qc_head_id:
        # Не считаем это ошибкой раннера: на dev-машине Битрикса нет.
        log.warning("Низких оценок к разбору: %d, но задачи не создаются "
                    "(нет BITRIX_WEBHOOK_URL или BITRIX_QC_HEAD_ID)", len(due))
        return {"created": 0, "failed": 0, "due": len(due)}

    deadline = (datetime.now(timezone.utc)
                + timedelta(hours=settings.qc_task_deadline_hours)
                ).isoformat(timespec="seconds")

    for row in due:
        title, body = _qc_task_text(row)
        try:
            task_id = bitrix.create_task(title, body,
                                         settings.bitrix_qc_head_id, deadline)
            with _conn() as c:
                c.execute(
                    "UPDATE nps_followup SET qc_task_id = ?, last_error = '', "
                    "updated_at = ? WHERE nps_id = ?",
                    (task_id, _now_iso(), row["nps_id"]),
                )
            created += 1
        except bitrix.BitrixError as e:
            failed += 1
            with _conn() as c:
                c.execute(
                    "UPDATE nps_followup SET attempts = attempts + 1, "
                    "last_error = ?, updated_at = ? WHERE nps_id = ?",
                    (str(e)[:300], _now_iso(), row["nps_id"]),
                )
            log.warning("Задача по оценке %s не создана: %s", row["nps_id"], e)
            # Сеть легла — остальные попытки в этом прогоне бессмысленны.
            if "сеть недоступна" in str(e):
                break

    return {"created": created, "failed": failed, "due": len(due)}


# ---------------------------------------------------------------------------
# Рейтинг менеджеров
# ---------------------------------------------------------------------------
def month_key(dt: Optional[datetime] = None) -> str:
    dt = dt or datetime.now(timezone.utc)
    return f"{dt.year:04d}-{dt.month:02d}"


def previous_month_key(dt: Optional[datetime] = None) -> str:
    dt = dt or datetime.now(timezone.utc)
    y, m = (dt.year, dt.month - 1) if dt.month > 1 else (dt.year - 1, 12)
    return f"{y:04d}-{m:02d}"


def rating(year_month: str) -> dict:
    """Рейтинг менеджеров за месяц.

    Считаем штуки, а не сумму оценок: сколько лучших (9-10) и сколько низких
    (0-6). Итог = лучшие минус низкие. Нейтральные 7-8 не влияют ни на что.

    Сумма оценок сюда не годится: она вознаграждает объём. Менеджер с
    десятком клиентов набрал бы больше того, у кого клиентов трое, даже
    работая заметно хуже.
    """
    with _conn() as c:
        rows = c.execute(
            "SELECT manager_id, manager_name, score FROM nps_followup "
            "WHERE substr(score_created_at, 1, 7) = ?",
            (year_month,),
        ).fetchall()

    by_manager: dict[int, dict] = {}
    totals = {"top": 0, "neutral": 0, "low": 0, "net": 0, "scores": 0,
              "invalid": 0}

    for r in rows:
        mid = int(r["manager_id"] or 0)
        cat = category(int(r["score"]))
        if cat == "invalid":
            # Битая запись не должна ни помогать менеджеру, ни вредить ему.
            # Считаем отдельно, чтобы её было видно, а не молча выбрасываем.
            totals["invalid"] += 1
            continue
        m = by_manager.setdefault(mid, {
            "manager_id": mid,
            "manager_name": r["manager_name"] or ("" if mid else "Не определён"),
            "top": 0, "neutral": 0, "low": 0, "net": 0, "scores": 0,
        })
        if r["manager_name"] and not m["manager_name"]:
            m["manager_name"] = r["manager_name"]
        m[cat] += 1
        m["scores"] += 1
        totals[cat] += 1
        totals["scores"] += 1

    for m in by_manager.values():
        m["net"] = m["top"] - m["low"]
    totals["net"] = totals["top"] - totals["low"]

    # Порядок: итог, при равенстве — больше лучших, затем меньше низких.
    def key(m: dict) -> tuple:
        return (-m["net"], -m["top"], m["low"], m["manager_name"])

    # Лидер — только среди определённых менеджеров: «Не определён» это не
    # сотрудник, а дырка в данных, и награждать её нельзя. Плюс требуем хотя
    # бы одну лучшую оценку: месяц без похвал — не повод объявлять лидера,
    # даже если у остальных итог ещё хуже.
    named = [m for m in by_manager.values() if m["manager_id"] and m["top"]]
    named.sort(key=key)
    ranked = sorted(by_manager.values(), key=key)

    return {
        "year_month": year_month,
        "leader": named[0] if named else None,
        "managers": ranked,
        "totals": totals,
    }


def low_scores(year_month: str) -> list[dict]:
    """Низкие оценки месяца — для сводки в отчёте."""
    with _conn() as c:
        rows = c.execute(
            "SELECT nps_id, email, score, score_created_at, manager_name, "
            "manager_id, qc_task_id FROM nps_followup "
            "WHERE substr(score_created_at, 1, 7) = ? AND score <= ? "
            "ORDER BY score_created_at",
            (year_month, settings.qc_detractor_max),
        ).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Месячный отчёт
# ---------------------------------------------------------------------------
def report_sent(year_month: str) -> Optional[dict]:
    with _conn() as c:
        r = c.execute("SELECT * FROM nps_monthly_report WHERE year_month = ?",
                      (year_month,)).fetchone()
        return dict(r) if r else None


def _report_text(year_month: str, data: dict, lows: list[dict]) -> tuple[str, str]:
    t = data["totals"]
    leader = data["leader"]
    title = f"Оценки клиентов за {year_month}: итоги и рейтинг менеджеров"

    g = grades()
    best, worst = g["top_label"], g["low_label"]
    lines = [
        f"[B]Отчёт по оценкам клиентов за {year_month}[/B]", "",
        f"Всего оценок: {t['scores']}",
        f"Лучшие ({best}): {t['top']}",
        f"Низкие ({worst}): {t['low']}",
    ]
    if g["has_neutral"]:
        lines.append(f"Нейтральные ({g['neutral_label']}): {t['neutral']}")
    lines += [f"Итог по отделу: {t['net']:+d}", ""]

    if leader:
        lines += [
            f"[B]Лидер месяца: {leader['manager_name']}[/B]",
            f"Итог: {leader['net']:+d} · лучших {best}: {leader['top']} · "
            f"низких {worst}: {leader['low']}", "",
        ]
    else:
        lines += [f"Лидер не определён: за месяц нет оценок {best} "
                  "с известным менеджером.", ""]

    lines.append("[B]Рейтинг[/B]")
    if data["managers"]:
        for i, m in enumerate(data["managers"], 1):
            name = m["manager_name"] or f"ID {m['manager_id']}"
            lines.append(
                f"{i}. {name} — итог {m['net']:+d} "
                f"(лучших {m['top']}, низких {m['low']})")
    else:
        lines.append("Оценок за месяц не было.")

    if lows:
        lines += ["", f"[B]Низкие оценки ({len(lows)})[/B]"]
        for r in lows:
            when = (r["score_created_at"] or "").replace("T", " ")[:16]
            task = f", задача №{r['qc_task_id']}" if r["qc_task_id"] else \
                   ", задача не создана"
            who = r["manager_name"] or "менеджер не определён"
            lines.append(f"· {r['score']}/10 — {r['email']} ({when}, {who}{task})")

    # Жалобы — тот же контур качества, руководителю нужны обе картины сразу.
    from . import complaints
    cmp_rows = complaints.month_summary(year_month)
    if cmp_rows:
        overdue = [r for r in cmp_rows
                   if r["status"] in complaints.OPEN_STATUSES]
        lines += ["", f"[B]Жалобы ({len(cmp_rows)})[/B]"]
        for r in cmp_rows:
            when = (r["created_at"] or "").replace("T", " ")[:16]
            task = f", задача №{r['qc_task_id']}" if r["qc_task_id"] else ""
            lines.append(f"· {r['category_label']} — {r['email']} "
                         f"({when}, {r['status_label']}{task})")
        if overdue:
            lines.append(f"Не закрыто на момент отчёта: {len(overdue)}.")
    else:
        lines += ["", "[B]Жалобы[/B]", "За месяц жалоб не поступало."]

    lines += ["", f"Итог = количество лучших оценок ({best}) минус количество "
              f"низких ({worst}). Нейтральные не учитываются.",
              "Отчёт сформирован админ-панелью автоматически."]
    return title, "\n".join(lines)


def send_monthly_report(year_month: str, force: bool = False) -> dict:
    """Отправить отчёт за месяц руководителям. Повторно — только с force."""
    existing = report_sent(year_month)
    if existing and not force:
        return {"ok": True, "already_sent": True, "report": existing}

    data = rating(year_month)
    lows = low_scores(year_month)
    title, body = _report_text(year_month, data, lows)

    recipients = settings.monthly_report_recipients
    if not bitrix.configured() or not recipients:
        # Это не сбой отправки, а незаконченная настройка. Помечаем как
        # пропуск: иначе на ненастроенном сервере воркер возвращал бы ошибку
        # каждые 10 минут, и systemd показывал бы юнит упавшим — за таким
        # «алертом» перестают следить, и настоящий сбой пройдёт незамеченным.
        return {"ok": False, "already_sent": False,
                "skipped": "не задан BITRIX_WEBHOOK_URL или ID руководителей",
                "preview": {"title": title, "body": body}}

    task_ids, errors = [], []
    for user_id in recipients:
        try:
            task_ids.append(bitrix.create_task(title, body, user_id))
        except bitrix.BitrixError as e:
            errors.append(f"{user_id}: {e}")
            log.warning("Отчёт за %s не ушёл сотруднику %s: %s",
                        year_month, user_id, e)

    if not task_ids:
        return {"ok": False, "already_sent": False,
                "error": "; ".join(errors) or "задачи не созданы"}

    leader = data["leader"] or {}
    with _conn() as c:
        c.execute(
            "INSERT INTO nps_monthly_report "
            "(year_month, sent_at, task_ids, leader_manager_id, leader_name, "
            " leader_net, snapshot) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(year_month) DO UPDATE SET "
            " sent_at = excluded.sent_at, task_ids = excluded.task_ids, "
            " leader_manager_id = excluded.leader_manager_id, "
            " leader_name = excluded.leader_name, "
            " leader_net = excluded.leader_net, "
            " snapshot = excluded.snapshot",
            (year_month, _now_iso(), ",".join(str(t) for t in task_ids),
             leader.get("manager_id", 0), leader.get("manager_name", ""),
             leader.get("net", 0), json.dumps(data, ensure_ascii=False)),
        )

    return {"ok": True, "already_sent": False, "task_ids": task_ids,
            "errors": errors, "year_month": year_month}


def due_report_month(now: Optional[datetime] = None) -> Optional[str]:
    """За какой месяц пора отправлять отчёт. None — рано или уже отправлен."""
    now = now or datetime.now(timezone.utc)
    if now.day < settings.monthly_report_day:
        return None
    ym = previous_month_key(now)
    return None if report_sent(ym) else ym


# ---------------------------------------------------------------------------
# Для админки
# ---------------------------------------------------------------------------
def list_scores(kind: str = "", year_month: str = "",
                limit: int = 50, offset: int = 0) -> dict:
    """Оценки с тем, что мы про них знаем. kind: '' | low | neutral | top."""
    where, args = [], []
    if kind == "low":
        where.append("s.score <= ?")
        args.append(settings.qc_detractor_max)
    elif kind == "top":
        where.append("s.score >= ?")
        args.append(settings.qc_promoter_min)
    elif kind == "neutral":
        where.append("s.score > ? AND s.score < ?")
        args += [settings.qc_detractor_max, settings.qc_promoter_min]
    if year_month:
        where.append("substr(s.created_at, 1, 7) = ?")
        args.append(year_month)

    clause = (" WHERE " + " AND ".join(where)) if where else ""
    limit = max(1, min(int(limit), 500))
    offset = max(0, int(offset))

    with _conn() as c:
        total = c.execute(
            f"SELECT COUNT(*) n FROM nps_scores s{clause}", args).fetchone()["n"]
        rows = c.execute(
            "SELECT s.id, s.email, s.score, s.left_review, s.created_at, "
            "f.manager_id, f.manager_name, f.qc_task_id, f.attempts, f.last_error "
            "FROM nps_scores s "
            f"LEFT JOIN nps_followup f ON f.nps_id = s.id{clause} "
            "ORDER BY s.id DESC LIMIT ? OFFSET ?",
            (*args, limit, offset),
        ).fetchall()

    items = []
    for r in rows:
        d = dict(r)
        d["category"] = category(int(d["score"]))
        items.append(d)
    return {"items": items, "total": int(total), "limit": limit,
            "offset": offset, "grades": grades()}


def status() -> dict:
    """Состояние механики — для админки: что не разобрано, что не отправлено."""
    with _conn() as c:
        unlinked = c.execute(
            "SELECT COUNT(*) n FROM nps_scores s "
            "LEFT JOIN nps_followup f ON f.nps_id = s.id "
            "WHERE f.nps_id IS NULL").fetchone()["n"]
        stuck = c.execute(
            "SELECT COUNT(*) n FROM nps_followup "
            "WHERE score <= ? AND qc_task_id = 0 AND attempts >= ?",
            (settings.qc_detractor_max, settings.qc_task_max_attempts),
        ).fetchone()["n"]
        last = c.execute(
            "SELECT * FROM nps_monthly_report ORDER BY year_month DESC LIMIT 1"
        ).fetchone()

    return {
        "bitrix_configured": bitrix.configured(),
        "qc_head_set": bool(settings.bitrix_qc_head_id),
        "recipients": len(settings.monthly_report_recipients),
        "unlinked_scores": int(unlinked),
        "stuck_tasks": int(stuck),
        "last_report": dict(last) if last else None,
        "due_month": due_report_month(),
        "grades": grades(),
    }


def run_worker() -> dict:
    """Один полный прогон: разобрать оценки и жалобы, поставить задачи, при
    наступлении срока отправить месячный отчёт."""
    from . import complaints, stages, supervision  # локальный импорт: те
    # модули ничего не знают про quality, связь только здесь
    result = {
        "linked": process_new_scores(),
        "tasks": create_pending_tasks(),
        "complaints": complaints.process_pending(),
        # Снимок стадий обновляется по своему расписанию (раз в час),
        # частые вызовы он отбрасывает сам.
        "stages": stages.sync(),
        # Отказы и зависшие дела считаются по свежему снимку, поэтому строго
        # после него.
        "refusal_reasons": supervision.ask_refusal_reasons(),
        "stuck": supervision.alert_stuck_deals(),
    }
    ym = due_report_month()
    result["report"] = send_monthly_report(ym) if ym else {"skipped": True}
    return result
