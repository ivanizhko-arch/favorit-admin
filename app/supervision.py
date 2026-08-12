"""Отдел сопровождения: отказы по менеджерам и перехват зависших дел.

Три вещи:

1. **Сколько отказов у каждого менеджера — в штуках и в долях.** Доля важнее:
   у менеджера с 50 делами и 10 отказами это 20 %, а у менеджера с 10 делами
   и 5 отказами — 50 %. По абсолютным числам первый выглядит вдвое хуже,
   хотя работает вдвое лучше.

2. **Причина рядом с каждым отказом.** С пометкой, кто её назвал. Если
   отказы влияют на премию, а причину указывает сам менеджер, «клиент
   передумал» вытеснит всё остальное за месяц — поэтому источник хранится
   рядом со значением и виден в отчётах.

3. **Дела, зависшие на стадии.** Порог свой у каждой стадии: медиана её
   прохождения по вашим же данным, умноженная на коэффициент. Иначе
   «Реализация» с её месяцами считалась бы зависшей наравне со «Сбором
   документов».

Читает снимок сделок (`deal_snapshot`, `deal_stage_history`), который ведёт
`stages.py`. Свои таблицы — причины отказов и отметки о предупреждениях.
"""
import logging
import sqlite3
from datetime import datetime, timedelta, timezone

from . import bitrix, stages, store
from .config import settings

log = logging.getLogger(__name__)

# Справочник причин. Фиксирован в коде: свободный текст не агрегируется,
# а произвольный список из настроек сделал бы отчёты несравнимыми во времени.
REASONS = {
    "manager":      "Не устроил менеджер",
    "price":        "Цена или нет денег на процедуру",
    "changed_mind": "Передумал банкротиться",
    "competitor":   "Ушёл к конкуренту",
    "criteria":     "Не прошёл по нашим критериям",
    "lost_contact": "Пропал, не выходит на связь",
    "other":        "Другое",
}

# Единственная причина, которая относится к работе менеджера. Остальные —
# обстоятельства клиента или наше собственное решение, и вешать их на
# менеджера значит получить не метрику качества, а генератор обид.
MANAGER_FAULT = {"manager"}

# Кто назвал причину. Порядок — по убыванию достоверности.
SOURCES = {
    "client":  "со слов клиента",
    "qc":      "контроль качества",
    "manager": "со слов менеджера",
    "bitrix":  "из карточки Битрикса",
}


def _conn() -> sqlite3.Connection:
    return store.connect()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def init() -> None:
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS deal_refusal (
                deal_id INTEGER PRIMARY KEY,
                reason TEXT DEFAULT '',        -- код из REASONS, пусто = не выяснено
                comment TEXT DEFAULT '',
                source TEXT DEFAULT '',        -- кто назвал причину, см. SOURCES
                stated_by TEXT DEFAULT '',     -- кто внёс запись
                qc_task_id INTEGER DEFAULT 0,  -- задача «выяснить причину»
                attempts INTEGER DEFAULT 0,
                last_error TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_refusal_reason "
                  "ON deal_refusal (reason)")
        # Отметка, что по зависшему делу уже предупреждали. Ключ включает
        # стадию: дело переехало дальше и снова зависло — это новый повод,
        # а стоять на той же стадии второй месяц — не повод для второй задачи.
        c.execute("""
            CREATE TABLE IF NOT EXISTS deal_stuck_alert (
                deal_id INTEGER NOT NULL,
                stage_id TEXT NOT NULL,
                days_on_stage REAL NOT NULL,
                task_id INTEGER DEFAULT 0,
                alerted_at TEXT NOT NULL,
                PRIMARY KEY (deal_id, stage_id)
            )
        """)


# ---------------------------------------------------------------------------
# Отказы
# ---------------------------------------------------------------------------
def _failed_placeholders() -> tuple:
    failed = sorted(settings.failed_stages)
    return failed, ",".join("?" * len(failed)) if failed else "''"


def is_manager(user_id: int, card: dict) -> tuple[bool, str]:
    """Показывать ли этого человека в таблице менеджеров.

    Возвращает (показывать, причина скрытия). Причину сохраняем, чтобы
    в панели можно было написать, кого и почему убрали: молча пропавшие
    строки выглядят как потеря данных.
    """
    if not user_id:
        return True, ""          # «Не определён» — дырка в данных, оставляем
    if user_id in settings.manager_excluded:
        return False, "исключён вручную"
    if settings.manager_hide_inactive and not card.get("active", True):
        return False, "уволен"
    if (settings.manager_hide_non_employees
            and card.get("user_type", "employee") != "employee"):
        return False, "не сотрудник: робот или внешняя учётка"
    needle = settings.manager_position_contains.strip().lower()
    if needle and needle not in (card.get("position") or "").lower():
        return False, "должность не подходит под фильтр"
    departments = settings.manager_departments
    if departments:
        own = {d.strip() for d in (card.get("departments") or "").split(",")
               if d.strip()}
        if not (own & departments):
            return False, "другой отдел"
    return True, ""


def manager_stats() -> dict:
    """Таблица менеджеров: дела, отказы, доля отказов.

    Доля считается от всех дел менеджера, а не только от закрытых: иначе
    менеджер с одним завершённым делом и одним отказом получил бы 50 %.
    """
    failed, marks = _failed_placeholders()
    cards = stages.users()
    names = {uid: c["name"] for uid, c in cards.items()}

    with _conn() as c:
        rows = c.execute("SELECT assigned_by_id, stage_id FROM deal_snapshot"
                         ).fetchall()
        reasons = {int(r["deal_id"]): r["reason"] for r in c.execute(
            "SELECT deal_id, reason FROM deal_refusal WHERE reason != ''")}
        deal_managers = {int(r["deal_id"]): int(r["assigned_by_id"] or 0)
                         for r in c.execute(
                             "SELECT deal_id, assigned_by_id FROM deal_snapshot")}

    by_manager: dict[int, dict] = {}
    for r in rows:
        mid = int(r["assigned_by_id"] or 0)
        m = by_manager.setdefault(mid, {
            "manager_id": mid,
            "manager_name": names.get(mid) or ("Не определён" if not mid
                                               else f"ID {mid}"),
            "deals": 0, "refusals": 0, "in_progress": 0, "done": 0,
            "manager_fault": 0, "reason_unknown": 0,
        })
        m["deals"] += 1
        kind = stages.stage_kind(r["stage_id"])
        if kind == "failed":
            m["refusals"] += 1
        elif kind == "done":
            m["done"] += 1
        elif kind in ("work", "stuck"):
            m["in_progress"] += 1

    # Разбор причин: сколько отказов относится к работе менеджера и по
    # скольким причина ещё не выяснена.
    for deal_id, mid in deal_managers.items():
        if mid not in by_manager:
            continue
        code = reasons.get(deal_id)
        if code is None:
            continue
        if code in MANAGER_FAULT:
            by_manager[mid]["manager_fault"] += 1
    with _conn() as c:
        unknown = c.execute(
            f"SELECT d.assigned_by_id mid, COUNT(*) n FROM deal_snapshot d "
            f"LEFT JOIN deal_refusal r ON r.deal_id = d.deal_id "
            f"WHERE d.stage_id IN ({marks}) "
            "AND (r.reason IS NULL OR r.reason = '') "
            "GROUP BY d.assigned_by_id", failed).fetchall()
    for u in unknown:
        mid = int(u["mid"] or 0)
        if mid in by_manager:
            by_manager[mid]["reason_unknown"] = int(u["n"])

    items, hidden = [], []
    for m in by_manager.values():
        m["refusal_rate"] = (round(m["refusals"] * 100 / m["deals"], 1)
                             if m["deals"] else 0.0)
        card = cards.get(m["manager_id"], {})
        m["position"] = card.get("position", "")
        show, why = is_manager(m["manager_id"], card)
        (items if show else hidden).append({**m, "hidden_reason": why})
    # Сначала те, у кого доля отказов выше — ради них таблица и нужна.
    items.sort(key=lambda m: (-m["refusal_rate"], -m["refusals"],
                              m["manager_name"]))

    totals_deals = sum(m["deals"] for m in items)
    totals_ref = sum(m["refusals"] for m in items)
    return {
        "managers": items,
        # Скрытых показываем сводкой: иначе сумма по таблице не сойдётся
        # с числом сделок в Битриксе, и это выглядит как потеря данных.
        "hidden": sorted(hidden, key=lambda m: -m["deals"]),
        "hidden_totals": {
            "people": len(hidden),
            "deals": sum(m["deals"] for m in hidden),
            "refusals": sum(m["refusals"] for m in hidden),
        },
        "totals": {
            "deals": totals_deals,
            "refusals": totals_ref,
            "refusal_rate": (round(totals_ref * 100 / totals_deals, 1)
                             if totals_deals else 0.0),
            "reason_unknown": sum(m["reason_unknown"] for m in items),
        },
        "reasons": REASONS,
        "sources": SOURCES,
        "manager_fault_codes": sorted(MANAGER_FAULT),
    }


def refusals(manager_id: int = 0, reason: str = "", unknown_only: bool = False,
             limit: int = 50, offset: int = 0) -> dict:
    """Список отказов: у кого, на какой стадии, сколько прожило, почему."""
    failed, marks = _failed_placeholders()
    where = [f"d.stage_id IN ({marks})"]
    args: list = list(failed)
    if manager_id:
        where.append("d.assigned_by_id = ?")
        args.append(int(manager_id))
    if reason:
        where.append("r.reason = ?")
        args.append(reason)
    if unknown_only:
        where.append("(r.reason IS NULL OR r.reason = '')")
    clause = " WHERE " + " AND ".join(where)
    limit = max(1, min(int(limit), 500))
    offset = max(0, int(offset))

    with _conn() as c:
        total = c.execute(
            "SELECT COUNT(*) n FROM deal_snapshot d "
            f"LEFT JOIN deal_refusal r ON r.deal_id = d.deal_id{clause}",
            args).fetchone()["n"]
        rows = c.execute(
            "SELECT d.deal_id, d.stage_id, d.assigned_by_id, d.created_at, "
            " d.modified_at, r.reason, r.comment, r.source, r.stated_by, "
            " r.qc_task_id "
            "FROM deal_snapshot d "
            f"LEFT JOIN deal_refusal r ON r.deal_id = d.deal_id{clause} "
            "ORDER BY d.modified_at DESC LIMIT ? OFFSET ?",
            (*args, limit, offset)).fetchall()

    names = stages.user_names()
    snames = stages.stage_names()
    items = []
    for r in rows:
        created = stages._parse(r["created_at"])
        closed = stages._parse(r["modified_at"])
        mid = int(r["assigned_by_id"] or 0)
        items.append({
            "deal_id": int(r["deal_id"]),
            "manager_id": mid,
            "manager_name": names.get(mid) or ("Не определён" if not mid
                                               else f"ID {mid}"),
            "stage_id": r["stage_id"],
            "stage_name": snames.get(r["stage_id"], r["stage_id"]),
            "reason": r["reason"] or "",
            "reason_label": REASONS.get(r["reason"] or "", ""),
            "manager_fault": (r["reason"] or "") in MANAGER_FAULT,
            "comment": r["comment"] or "",
            "source": r["source"] or "",
            "source_label": SOURCES.get(r["source"] or "", ""),
            "stated_by": r["stated_by"] or "",
            "qc_task_id": int(r["qc_task_id"] or 0),
            "refused_at": r["modified_at"],
            "days_lived": (round((closed - created).total_seconds() / 86400, 1)
                           if created and closed and closed >= created else None),
        })
    return {"items": items, "total": int(total), "limit": limit,
            "offset": offset, "reasons": REASONS, "sources": SOURCES}


def set_reason(deal_id: int, reason: str, comment: str = "",
               source: str = "qc", stated_by: str = "admin") -> dict:
    if reason not in REASONS:
        raise ValueError("Неизвестная причина отказа")
    if source not in SOURCES:
        raise ValueError("Неизвестный источник причины")
    now = _iso(_now())
    with _conn() as c:
        c.execute(
            "INSERT INTO deal_refusal (deal_id, reason, comment, source, "
            " stated_by, created_at, updated_at) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(deal_id) DO UPDATE SET reason = excluded.reason, "
            " comment = excluded.comment, source = excluded.source, "
            " stated_by = excluded.stated_by, updated_at = excluded.updated_at",
            (int(deal_id), reason, comment[:2000], source, stated_by, now, now))
    return {"ok": True, "deal_id": int(deal_id), "reason": reason}


def import_reasons_from_bitrix(deals: list[dict]) -> int:
    """Забрать причину из поля сделки, если оно у вас уже ведётся.

    Значение поля кладём как есть, с источником «из карточки Битрикса»: оно
    не из нашего справочника, и притворяться, что из него, нельзя.
    """
    field = settings.bitrix_refusal_reason_field.strip()
    if not field:
        return 0
    now = _iso(_now())
    taken = 0
    for d in deals:
        value = str(d.get(field) or "").strip()
        if not value:
            continue
        with _conn() as c:
            c.execute(
                "INSERT INTO deal_refusal (deal_id, reason, comment, source, "
                " stated_by, created_at, updated_at) "
                "VALUES (?, '', ?, 'bitrix', ?, ?, ?) "
                "ON CONFLICT(deal_id) DO UPDATE SET "
                # Заполненную вручную причину не затираем: она достовернее.
                " comment = CASE WHEN deal_refusal.source = 'bitrix' "
                "   THEN excluded.comment ELSE deal_refusal.comment END, "
                " updated_at = excluded.updated_at",
                (int(d.get("ID") or 0), value[:2000], field, now, now))
        taken += 1
    return taken


# ---------------------------------------------------------------------------
# Зависшие дела
# ---------------------------------------------------------------------------
def stage_thresholds() -> dict:
    """Сколько дней на стадии считать нормой. Порог свой у каждой стадии."""
    medians = stages.stage_medians()
    out = {}
    for stage_id in stages.stage_names():
        kind = stages.stage_kind(stage_id)
        if kind in ("done", "failed", "service"):
            continue          # там дело и должно стоять — оно закончено
        median = medians.get(stage_id)
        if median:
            out[stage_id] = max(median * settings.stuck_factor,
                                float(settings.stuck_min_days))
        else:
            # Завершённых прохождений мало — медиане верить нельзя.
            out[stage_id] = float(settings.stuck_default_days)
    return out


def stuck_deals(limit: int = 100) -> dict:
    """Дела, стоящие на стадии дольше нормы. Сначала самые запущенные."""
    thresholds = stage_thresholds()
    names = stages.user_names()
    snames = stages.stage_names()
    medians = stages.stage_medians()

    with _conn() as c:
        alerted = {(int(r["deal_id"]), r["stage_id"])
                   for r in c.execute(
                       "SELECT deal_id, stage_id FROM deal_stuck_alert")}

    items = []
    for d in stages.deals_on_current_stage():
        limit_days = thresholds.get(d["stage_id"])
        if not limit_days or d["days_on_stage"] <= limit_days:
            continue
        mid = d["manager_id"]
        items.append({
            **d,
            "manager_name": names.get(mid) or ("Не определён" if not mid
                                               else f"ID {mid}"),
            "stage_name": snames.get(d["stage_id"], d["stage_id"]),
            "limit_days": round(limit_days, 1),
            "median_days": (round(medians[d["stage_id"]], 1)
                            if d["stage_id"] in medians else None),
            "over_days": round(d["days_on_stage"] - limit_days, 1),
            "alerted": (d["deal_id"], d["stage_id"]) in alerted,
        })
    items.sort(key=lambda x: -x["over_days"])
    return {"items": items[:limit], "total": len(items),
            "thresholds": {k: round(v, 1) for k, v in thresholds.items()}}


# ---------------------------------------------------------------------------
# Задачи: выяснить причину и перехватить зависшее дело
# ---------------------------------------------------------------------------
def _task_ready(responsible: int) -> bool:
    return bool(bitrix.configured() and responsible)


def _baseline_needed(fresh_count: int) -> bool:
    """Нужно ли сначала отметить накопленное, не создавая задач.

    Срабатывает один раз: пока в журнале предупреждений пусто, а зависших
    дел разом больше порога. Дальше журнал не пуст, и всё идёт обычным
    порядком — по новым зависаниям.
    """
    if fresh_count <= settings.stuck_baseline_threshold:
        return False
    with _conn() as c:
        seen = c.execute("SELECT COUNT(*) n FROM deal_stuck_alert"
                         ).fetchone()["n"]
    return int(seen) == 0


def ask_refusal_reasons() -> dict:
    """По каждому новому отказу без причины — задача контролю качества.

    Причину надёжнее узнать у клиента, чем у менеджера, чей отказ разбирают.
    Механика та же, что уже работает для низких оценок.
    """
    failed, marks = _failed_placeholders()
    # Отсечка по возрасту. В воронке лежат отказы за все годы работы, и без
    # неё первый запуск поставил бы задачу по каждому: спрашивать причину
    # у клиента, ушедшего три года назад, бессмысленно.
    cutoff = _iso(_now() - timedelta(
        days=settings.refusal_reason_max_age_days))
    with _conn() as c:
        rows = c.execute(
            "SELECT d.deal_id, d.assigned_by_id, d.stage_id "
            "FROM deal_snapshot d LEFT JOIN deal_refusal r "
            "  ON r.deal_id = d.deal_id "
            f"WHERE d.stage_id IN ({marks}) "
            "AND d.modified_at >= ? "
            "AND (r.reason IS NULL OR r.reason = '') "
            "AND (r.qc_task_id IS NULL OR r.qc_task_id = 0) "
            "AND (r.attempts IS NULL OR r.attempts < ?) "
            "ORDER BY d.modified_at DESC LIMIT ?",
            (*failed, cutoff, settings.qc_task_max_attempts,
             settings.stuck_alert_batch)).fetchall()
    due = [dict(r) for r in rows]
    if not due:
        return {"created": 0, "failed": 0, "due": 0}
    if not _task_ready(settings.bitrix_qc_head_id):
        log.warning("Отказов без причины: %d, но задачи не создаются "
                    "(нет BITRIX_WEBHOOK_URL или BITRIX_QC_HEAD_ID)", len(due))
        return {"created": 0, "failed": 0, "due": len(due)}

    names, snames = stages.user_names(), stages.stage_names()
    created = failed_n = 0
    now = _iso(_now())
    for d in due:
        mid = int(d["assigned_by_id"] or 0)
        manager = names.get(mid) or (f"ID {mid}" if mid else "не определён")
        title = f"Выяснить причину отказа: сделка №{d['deal_id']}"
        body = (
            f"Сделка [B]№{d['deal_id']}[/B] перешла в стадию "
            f"«{snames.get(d['stage_id'], d['stage_id'])}».\n"
            f"Ответственный менеджер: {manager}\n\n"
            "Просьба связаться с клиентом и выяснить причину отказа, затем "
            "указать её в админ-панели, раздел «Отказы».\n\n"
            "Причина со слов клиента достовернее, чем со слов менеджера: "
            "именно поэтому спрашиваем отдельно."
        )
        try:
            task_id = bitrix.create_task(title, body,
                                         settings.bitrix_qc_head_id)
            with _conn() as c:
                c.execute(
                    "INSERT INTO deal_refusal (deal_id, qc_task_id, "
                    " created_at, updated_at) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(deal_id) DO UPDATE SET "
                    " qc_task_id = excluded.qc_task_id, last_error = '', "
                    " updated_at = excluded.updated_at",
                    (d["deal_id"], task_id, now, now))
            created += 1
        except bitrix.BitrixError as e:
            failed_n += 1
            with _conn() as c:
                c.execute(
                    "INSERT INTO deal_refusal (deal_id, attempts, last_error, "
                    " created_at, updated_at) VALUES (?, 1, ?, ?, ?) "
                    "ON CONFLICT(deal_id) DO UPDATE SET "
                    " attempts = COALESCE(deal_refusal.attempts, 0) + 1, "
                    " last_error = excluded.last_error, "
                    " updated_at = excluded.updated_at",
                    (d["deal_id"], str(e)[:300], now, now))
            log.warning("Задача по отказу %s не создана: %s", d["deal_id"], e)
            if "сеть недоступна" in str(e):
                break
    return {"created": created, "failed": failed_n, "due": len(due)}


def alert_stuck_deals() -> dict:
    """Перехват: по зависшему делу — задача руководителю сопровождения.

    Задача ставится один раз на пару «дело + стадия». Дело поехало дальше
    и снова зависло — это новый повод; стоять на той же стадии второй месяц
    поводом для второй задачи не является.
    """
    data = stuck_deals(limit=10000)
    fresh_all = [d for d in data["items"] if not d["alerted"]]

    # Первый запуск на накопленной базе. Дела зависали годами, и задача по
    # каждому — это не помощь, а поток, который закроют не читая. Отмечаем
    # их как известные и молчим; накопленное разбирается по таблице в
    # панели, а задачи пойдут по тем, кто зависнет дальше.
    if _baseline_needed(len(fresh_all)):
        now = _iso(_now())
        with _conn() as c:
            for d in fresh_all:
                c.execute(
                    "INSERT OR REPLACE INTO deal_stuck_alert "
                    "(deal_id, stage_id, days_on_stage, task_id, alerted_at) "
                    "VALUES (?, ?, ?, 0, ?)",
                    (d["deal_id"], d["stage_id"], d["days_on_stage"], now))
        log.warning("Первый прогон: %d зависших дел отмечены без задач. "
                    "Разбирать их следует по таблице в админ-панели.",
                    len(fresh_all))
        return {"created": 0, "failed": 0, "due": 0, "baseline": len(fresh_all),
                "stuck_total": data["total"]}

    fresh = fresh_all[:settings.stuck_alert_batch]
    if not fresh:
        return {"created": 0, "failed": 0, "due": 0, "stuck_total": data["total"]}

    responsible = settings.bitrix_support_head_id
    if not _task_ready(responsible):
        log.warning("Зависших дел: %d, но задачи не создаются "
                    "(нет BITRIX_WEBHOOK_URL или BITRIX_SUPPORT_HEAD_ID)",
                    len(fresh))
        return {"created": 0, "failed": 0, "due": len(fresh),
                "stuck_total": data["total"]}

    created = failed_n = 0
    now = _iso(_now())
    for d in fresh:
        norm = (f"обычно эта стадия занимает около {d['median_days']} дн."
                if d["median_days"] else
                f"порог по умолчанию — {int(d['limit_days'])} дн.")
        title = (f"Дело №{d['deal_id']} стоит {int(d['days_on_stage'])} дн. "
                 f"на стадии «{d['stage_name']}»")
        body = (
            f"Сделка [B]№{d['deal_id']}[/B] не двигается "
            f"[B]{int(d['days_on_stage'])} дн.[/B]\n"
            f"Стадия: {d['stage_name']}\n"
            f"Ответственный: {d['manager_name']}\n"
            f"Норма: {norm} Превышение: {int(d['over_days'])} дн.\n\n"
            "Просьба разобраться, что мешает делу двигаться, и при "
            "необходимости вмешаться. Дело в этом состоянии — кандидат "
            "на отказ клиента."
        )
        try:
            task_id = bitrix.create_task(title, body, responsible)
            with _conn() as c:
                c.execute(
                    "INSERT OR REPLACE INTO deal_stuck_alert "
                    "(deal_id, stage_id, days_on_stage, task_id, alerted_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (d["deal_id"], d["stage_id"], d["days_on_stage"],
                     task_id, now))
            created += 1
        except bitrix.BitrixError as e:
            failed_n += 1
            log.warning("Задача по зависшему делу %s не создана: %s",
                        d["deal_id"], e)
            if "сеть недоступна" in str(e):
                break
    return {"created": created, "failed": failed_n, "due": len(fresh),
            "stuck_total": data["total"]}


def status() -> dict:
    failed, marks = _failed_placeholders()
    with _conn() as c:
        total_ref = c.execute(
            f"SELECT COUNT(*) n FROM deal_snapshot WHERE stage_id IN ({marks})",
            failed).fetchone()["n"]
        unknown = c.execute(
            "SELECT COUNT(*) n FROM deal_snapshot d LEFT JOIN deal_refusal r "
            f"ON r.deal_id = d.deal_id WHERE d.stage_id IN ({marks}) "
            "AND (r.reason IS NULL OR r.reason = '')", failed).fetchone()["n"]
    stuck = stuck_deals(limit=1)
    return {
        "refusals": int(total_ref),
        "reason_unknown": int(unknown),
        "stuck": stuck["total"],
        "qc_head_set": bool(settings.bitrix_qc_head_id),
        "support_head_set": bool(settings.bitrix_support_head_id),
        "bitrix_configured": bitrix.configured(),
    }
