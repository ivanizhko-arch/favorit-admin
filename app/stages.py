"""Стадии банкротства: снимок из Битрикса и метрики по срокам.

Считаем три вещи, запрошенные бизнесом:
  1. средний срок банкротства целиком;
  2. сколько клиентов сейчас на каждой стадии;
  3. средний срок прохождения каждой стадии.

Данных о стадиях в общей базе нет — они живут в воронке сделок Битрикса
(направление «БФЛ. Сопровождение», CATEGORY_ID=15). Считать их запросами
на лету нельзя: это сотни обращений и секунды ожидания при каждом открытии
админки. Поэтому здесь локальный снимок, который обновляет воркер, а
страница читает готовые цифры.

Методика (согласована с заказчиком, подписана в интерфейсе):

* **Завершение дела — стадия «Долг списан»**, а не закрытие сделки: сделка
  может висеть открытой ещё долго после списания.
* **Средние считаем только по завершённым** — прохождениям стадии, которые
  уже закончились, и делам, дошедшим до списания. Если подмешать тех, кто
  сидит на стадии прямо сейчас, среднее занизится: их срок ещё не истёк.
* **Рядом со средним показываем медиану.** Одно дело на три года перекашивает
  среднее так, что оно перестаёт описывать типичный случай.
* **Стадии ожидания считаем отдельно** от рабочих: «Пауза» и «Заседание
  отложено» — это простой, а не работа, и смешивать их в один средний срок
  значит скрывать, где именно дело стоит.

Таблицы собственные, общая схема не затронута.
"""
import logging
import sqlite3
import statistics
from datetime import datetime, timedelta, timezone
from typing import Optional

from . import bitrix, store
from .config import settings

log = logging.getLogger(__name__)

# Порог, ниже которого среднее не показываем: по двум делам «средний срок»
# это не статистика, а случайность.
MIN_SAMPLE = 3

# По сколько строк писать за одну транзакцию. Блокировка на запись держится
# всё время транзакции, и сбросить 20 000 записей истории одним куском значит
# на это время лишить записи админку и основной сервис.
WRITE_CHUNK = 200


def _chunks(rows: list, size: int = WRITE_CHUNK):
    for i in range(0, len(rows), size):
        yield rows[i:i + size]


def _conn() -> sqlite3.Connection:
    return store.connect()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _parse(value) -> Optional[datetime]:
    """Битрикс отдаёт даты в ISO с часовым поясом (2026-07-30T12:00:00+03:00).
    Приводим к UTC, иначе разница дат врала бы на часы."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def init() -> None:
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS deal_stage_dict (
                stage_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                sort INTEGER DEFAULT 0,
                semantics TEXT DEFAULT '',
                updated_at TEXT NOT NULL
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS deal_snapshot (
                deal_id INTEGER PRIMARY KEY,
                stage_id TEXT NOT NULL,
                contact_id INTEGER DEFAULT 0,
                assigned_by_id INTEGER DEFAULT 0,
                created_at TEXT,
                modified_at TEXT,
                closed_at TEXT,
                synced_at TEXT NOT NULL
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_deal_stage "
                  "ON deal_snapshot (stage_id)")
        c.execute("""
            CREATE TABLE IF NOT EXISTS deal_stage_history (
                deal_id INTEGER NOT NULL,
                stage_id TEXT NOT NULL,
                entered_at TEXT NOT NULL,
                PRIMARY KEY (deal_id, stage_id, entered_at)
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_hist_deal "
                  "ON deal_stage_history (deal_id, entered_at)")
        # Отметки последней синхронизации, чтобы тянуть только изменившееся.
        c.execute("""
            CREATE TABLE IF NOT EXISTS stage_sync_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        # Имена сотрудников. Без кэша таблица менеджеров дёргала бы Битрикс
        # на каждую загрузку страницы — по запросу на человека.
        c.execute("""
            CREATE TABLE IF NOT EXISTS bitrix_user (
                user_id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        # Должность, увольнение и тип учётки — по ним отсеиваются те, кто
        # менеджером не является. Таблица уже существует на сервере,
        # поэтому колонки добавляются отдельно.
        # Тип колонки обязателен: SQLite позволяет ADD COLUMN без типа,
        # PostgreSQL — нет (syntax error at or near "DEFAULT").
        for column, coltype, default in (
            ("position", "TEXT", "''"),
            ("active", "INTEGER", "1"),
            ("user_type", "TEXT", "'employee'"),
            ("departments", "TEXT", "''"),
        ):
            try:
                c.execute(
                    f"ALTER TABLE bitrix_user ADD COLUMN {column} "
                    f"{coltype} DEFAULT {default}"
                )
            except sqlite3.OperationalError:
                pass  # колонка уже есть


def _state(key: str) -> str:
    with _conn() as c:
        r = c.execute("SELECT value FROM stage_sync_state WHERE key = ?",
                      (key,)).fetchone()
        return r["value"] if r else ""


def _set_state(key: str, value: str) -> None:
    with _conn() as c:
        c.execute("INSERT INTO stage_sync_state (key, value) VALUES (?, ?) "
                  "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                  (key, value))


# ---------------------------------------------------------------------------
# Синхронизация
# ---------------------------------------------------------------------------
def sync(force: bool = False) -> dict:
    """Обновить снимок сделок и историю стадий."""
    if not bitrix.configured():
        return {"skipped": "Битрикс не настроен"}

    last = _parse(_state("last_sync"))
    if last and not force:
        age = (_now() - last).total_seconds() / 60
        if age < settings.stage_sync_min_interval_minutes:
            return {"skipped": f"обновлялось {int(age)} мин назад"}

    cat = settings.bankruptcy_category_id
    result: dict = {"stages": 0, "deals": 0, "history": 0}

    # Справочник стадий — маленький, тянем целиком каждый раз.
    try:
        stages = bitrix.deal_stages(cat)
    except bitrix.BitrixError as e:
        log.warning("Справочник стадий не получен: %s", e)
        return {"error": str(e)}
    now = _iso(_now())
    with _conn() as c:
        for s in stages:
            c.execute(
                "INSERT INTO deal_stage_dict (stage_id, name, sort, semantics, "
                "updated_at) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(stage_id) DO UPDATE SET name = excluded.name, "
                "sort = excluded.sort, semantics = excluded.semantics, "
                "updated_at = excluded.updated_at",
                (s["stage_id"], s["name"], s["sort"], s["semantics"], now))
    result["stages"] = len(stages)

    # Сделки — инкрементально по дате изменения. Небольшой нахлёст назад,
    # чтобы не потерять записи, изменённые в ту же секунду, что и срез.
    since = _state("deals_modified_to")
    if since and not force:
        overlap = _parse(since)
        since = _iso(overlap - timedelta(minutes=5)) if overlap else ""
    try:
        rows = bitrix.deals(cat, modified_since=since,
                            limit=settings.stage_sync_max_deals)
    except bitrix.BitrixError as e:
        log.warning("Сделки не получены: %s", e)
        return {"error": str(e), **result}

    newest = since
    for chunk in _chunks(rows):
        with _conn() as c:
            for d in chunk:
                created = _parse(d.get("DATE_CREATE"))
                modified = _parse(d.get("DATE_MODIFY"))
                closed = _parse(d.get("CLOSEDATE"))
                c.execute(
                    "INSERT INTO deal_snapshot (deal_id, stage_id, contact_id, "
                    " assigned_by_id, created_at, modified_at, closed_at, synced_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(deal_id) DO UPDATE SET stage_id = excluded.stage_id, "
                    " contact_id = excluded.contact_id, "
                    " assigned_by_id = excluded.assigned_by_id, "
                    " modified_at = excluded.modified_at, "
                    " closed_at = excluded.closed_at, synced_at = excluded.synced_at",
                    (int(d.get("ID") or 0), d.get("STAGE_ID") or "",
                     int(d.get("CONTACT_ID") or 0), int(d.get("ASSIGNED_BY_ID") or 0),
                     _iso(created) if created else None,
                     _iso(modified) if modified else None,
                     _iso(closed) if closed else None, now))
                if modified and (not newest or _iso(modified) > newest):
                    newest = _iso(modified)
    result["deals"] = len(rows)
    if newest:
        _set_state("deals_modified_to", newest)

    # История переходов — тоже инкрементально.
    hsince = "" if force else _state("history_to")
    try:
        hist = bitrix.stage_history(cat, since=hsince)
    except bitrix.BitrixError as e:
        log.warning("История стадий не получена: %s", e)
        _set_state("last_sync", now)
        return {"error": str(e), **result}

    newest_h = hsince
    for chunk in _chunks(hist):
        with _conn() as c:
            for h in chunk:
                at = _parse(h.get("CREATED_TIME"))
                if not at:
                    continue
                c.execute(
                    "INSERT OR IGNORE INTO deal_stage_history "
                    "(deal_id, stage_id, entered_at) VALUES (?, ?, ?)",
                    (int(h.get("OWNER_ID") or 0), h.get("STAGE_ID") or "", _iso(at)))
                if not newest_h or _iso(at) > newest_h:
                    newest_h = _iso(at)
    result["history"] = len(hist)
    if newest_h:
        _set_state("history_to", newest_h)

    _sync_user_names()
    _set_state("last_sync", now)
    return result


def _sync_user_names() -> int:
    """Обновить карточки ответственных: имя, должность, увольнение.

    Новых спрашиваем всегда, известных — раз в сутки. Увольнения и смена
    должности случаются, и кэш, который никогда не обновляется, однажды
    покажет уволенного как действующего менеджера.
    """
    stale = _iso(_now() - timedelta(days=1))
    with _conn() as c:
        rows = c.execute(
            "SELECT DISTINCT d.assigned_by_id uid FROM deal_snapshot d "
            "LEFT JOIN bitrix_user u ON u.user_id = d.assigned_by_id "
            "WHERE d.assigned_by_id != 0 "
            "AND (u.user_id IS NULL OR u.updated_at < ?)", (stale,)).fetchall()
    added = 0
    for r in rows:
        uid = int(r["uid"])
        try:
            info = bitrix.user_info(uid)
        except Exception as e:
            # Карточка сотрудника — украшение таблицы. Что бы ни случилось
            # при её получении, синхронизация сделок падать не должна.
            log.warning("Карточка сотрудника %s не получена: %s", uid, e)
            continue
        if not info:
            continue
        with _conn() as c:
            c.execute(
                "INSERT INTO bitrix_user (user_id, name, position, active, "
                " user_type, departments, updated_at) VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET name = excluded.name, "
                " position = excluded.position, active = excluded.active, "
                " user_type = excluded.user_type, "
                " departments = excluded.departments, "
                " updated_at = excluded.updated_at",
                (uid, info["name"], info["position"], 1 if info["active"] else 0,
                 info["user_type"], info["departments"], _iso(_now())))
        added += 1
    return added


def user_names() -> dict:
    """Кэш имён: {id: имя}. Пусто — покажем «ID N», это не повод падать."""
    with _conn() as c:
        return {int(r["user_id"]): r["name"]
                for r in c.execute("SELECT user_id, name FROM bitrix_user")}


def users() -> dict:
    """Полные карточки: {id: {name, position, active, user_type, departments}}."""
    with _conn() as c:
        rows = c.execute(
            "SELECT user_id, name, position, active, user_type, departments "
            "FROM bitrix_user").fetchall()
    return {int(r["user_id"]): {
        "name": r["name"],
        "position": r["position"] or "",
        "active": bool(r["active"]),
        "user_type": r["user_type"] or "employee",
        "departments": r["departments"] or "",
    } for r in rows}


def stage_names() -> dict:
    with _conn() as c:
        return {r["stage_id"]: r["name"]
                for r in c.execute("SELECT stage_id, name FROM deal_stage_dict")}


def stage_medians() -> dict:
    """Медиана прохождения по каждой стадии — основа порога «зависания».
    У каждой стадии своя норма, взятая из ваших же данных."""
    out = {}
    for stage_id, values in _passes().items():
        if len(values) >= MIN_SAMPLE:
            out[stage_id] = statistics.median(values)
    return out


def deals_on_current_stage() -> list[dict]:
    """Дела и сколько дней каждое стоит на своей нынешней стадии."""
    now = _now()
    with _conn() as c:
        # PostgreSQL требует все non-aggregate поля в GROUP BY.
        # s.deal_id уникален в snapshot'e — поэтому остальные поля
        # (stage_id, assigned_by_id, created_at) однозначны в группе.
        rows = c.execute(
            "SELECT s.deal_id, s.stage_id, s.assigned_by_id, s.created_at, "
            "       MAX(h.entered_at) entered "
            "FROM deal_snapshot s LEFT JOIN deal_stage_history h "
            "  ON h.deal_id = s.deal_id AND h.stage_id = s.stage_id "
            "GROUP BY s.deal_id, s.stage_id, s.assigned_by_id, s.created_at"
        ).fetchall()
    out = []
    for r in rows:
        entered = _parse(r["entered"]) or _parse(r["created_at"])
        if not entered:
            continue
        out.append({
            "deal_id": int(r["deal_id"]),
            "stage_id": r["stage_id"],
            "manager_id": int(r["assigned_by_id"] or 0),
            "entered_at": _iso(entered),
            "days_on_stage": round((now - entered).total_seconds() / 86400, 1),
            "created_at": r["created_at"],
        })
    return out


# ---------------------------------------------------------------------------
# Метрики
# ---------------------------------------------------------------------------
def _stage_dict() -> dict:
    with _conn() as c:
        rows = c.execute(
            "SELECT stage_id, name, sort, semantics FROM deal_stage_dict "
            "ORDER BY sort").fetchall()
    return {r["stage_id"]: dict(r) for r in rows}


def stage_kind(stage_id: str) -> str:
    """Роль стадии: work | stuck | failed | service | done."""
    # «Долг списан» и всё, что после него: дело закончено. Без этого сделка,
    # доведённая до «Успешно реализовано», вечно числилась бы в работе.
    if stage_id == settings.stage_done_id or stage_id in settings.success_stages:
        return "done"
    if stage_id in settings.service_stages:
        return "service"
    if stage_id in settings.stuck_stages:
        return "stuck"
    if stage_id in settings.failed_stages:
        return "failed"
    return "work"


def _summary(values: list[float]) -> dict:
    """Среднее и медиана. Медиана рядом не для красоты: одно затянувшееся
    дело сдвигает среднее так, что оно перестаёт описывать типичный случай."""
    if not values:
        return {"count": 0, "avg_days": None, "median_days": None,
                "max_days": None, "enough": False}
    return {
        "count": len(values),
        "avg_days": round(sum(values) / len(values), 1),
        "median_days": round(statistics.median(values), 1),
        "max_days": round(max(values), 1),
        "enough": len(values) >= MIN_SAMPLE,
    }


def _passes() -> dict:
    """Завершённые прохождения стадий: {stage_id: [длительность в днях]}.

    Длительность — промежуток между входом в стадию и следующим переходом.
    Последняя запись по сделке не считается: клиент на этой стадии сейчас,
    его срок ещё не закончился.
    """
    with _conn() as c:
        rows = c.execute(
            "SELECT deal_id, stage_id, entered_at FROM deal_stage_history "
            "ORDER BY deal_id, entered_at").fetchall()

    by_deal: dict[int, list] = {}
    for r in rows:
        by_deal.setdefault(int(r["deal_id"]), []).append(
            (r["stage_id"], _parse(r["entered_at"])))

    out: dict[str, list[float]] = {}
    for entries in by_deal.values():
        for i in range(len(entries) - 1):
            stage, start = entries[i]
            _, end = entries[i + 1]
            if not start or not end or end < start:
                continue
            out.setdefault(stage, []).append((end - start).total_seconds() / 86400)
    return out


def _current_on_stage() -> dict:
    """Сколько дел сейчас на каждой стадии и сколько уже ждут."""
    now = _now()
    with _conn() as c:
        rows = c.execute(
            "SELECT stage_id, COUNT(*) n FROM deal_snapshot GROUP BY stage_id"
        ).fetchall()
        counts = {r["stage_id"]: int(r["n"]) for r in rows}
        # Когда дело попало на текущую стадию — последняя запись истории.
        waits = c.execute(
            "SELECT s.stage_id, MAX(h.entered_at) entered "
            "FROM deal_snapshot s JOIN deal_stage_history h "
            "  ON h.deal_id = s.deal_id AND h.stage_id = s.stage_id "
            "GROUP BY s.deal_id, s.stage_id").fetchall()
    longest: dict[str, float] = {}
    for w in waits:
        at = _parse(w["entered"])
        if not at:
            continue
        days = (now - at).total_seconds() / 86400
        sid = w["stage_id"]
        if days > longest.get(sid, -1):
            longest[sid] = days
    return {"counts": counts, "longest": longest}


def funnel() -> dict:
    """Воронка: стадии в порядке Битрикса с количеством дел и сроками."""
    stages = _stage_dict()
    passes = _passes()
    cur = _current_on_stage()

    items = []
    for sid, meta in stages.items():
        kind = stage_kind(sid)
        if kind == "service":
            continue  # тех.этап и маркеры — не этап работы
        s = _summary(passes.get(sid, []))
        items.append({
            "stage_id": sid,
            "name": meta["name"],
            "sort": meta["sort"],
            "kind": kind,
            "alternative": sid in settings.alternative_stages,
            "current": cur["counts"].get(sid, 0),
            "longest_wait_days": (round(cur["longest"][sid], 1)
                                  if sid in cur["longest"] else None),
            **s,
        })
    return {"stages": items,
            "total_on_stages": sum(i["current"] for i in items)}


def overall() -> dict:
    """Средний срок банкротства: от создания дела до стадии «Долг списан».

    Считаем только по дошедшим до списания. Незавершённые исключены: их срок
    ещё не истёк, и включение занизило бы среднее.
    """
    done_stage = settings.stage_done_id
    with _conn() as c:
        # PostgreSQL требует все non-aggregate поля в GROUP BY (SQLite
        # позволял implicit-first). Группируем и по deal_id, и по
        # created_at — deal_id уникален в snapshot'e, значит created_at
        # тоже уникален в контексте группы.
        rows = c.execute(
            "SELECT h.deal_id, MIN(h.entered_at) done_at, d.created_at "
            "FROM deal_stage_history h JOIN deal_snapshot d "
            "  ON d.deal_id = h.deal_id "
            "WHERE h.stage_id = ? GROUP BY h.deal_id, d.created_at",
            (done_stage,)).fetchall()
        total_deals = c.execute("SELECT COUNT(*) n FROM deal_snapshot").fetchone()["n"]

    durations = []
    for r in rows:
        start, end = _parse(r["created_at"]), _parse(r["done_at"])
        if start and end and end >= start:
            durations.append((end - start).total_seconds() / 86400)

    # Сколько дел в работе прямо сейчас — то есть не завершены и не провалены.
    with _conn() as c:
        snap = c.execute("SELECT stage_id FROM deal_snapshot").fetchall()
    in_progress = sum(1 for s in snap
                      if stage_kind(s["stage_id"]) in ("work", "stuck"))
    failed = sum(1 for s in snap if stage_kind(s["stage_id"]) == "failed")

    return {"done": _summary(durations), "total_deals": int(total_deals),
            "in_progress": in_progress, "failed": failed,
            "done_stage_name": _stage_dict().get(done_stage, {}).get(
                "name", done_stage)}


def status() -> dict:
    last = _state("last_sync")
    with _conn() as c:
        deals = c.execute("SELECT COUNT(*) n FROM deal_snapshot").fetchone()["n"]
        hist = c.execute("SELECT COUNT(*) n FROM deal_stage_history").fetchone()["n"]
        stages = c.execute("SELECT COUNT(*) n FROM deal_stage_dict").fetchone()["n"]
    return {"configured": bool(bitrix.configured()),
            "category_id": settings.bankruptcy_category_id,
            "last_sync": last, "deals": int(deals),
            "history_rows": int(hist), "stages": int(stages),
            "min_sample": MIN_SAMPLE}
