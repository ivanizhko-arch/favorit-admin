"""Админ-панель приложения: API для управления пользователями + сама страница."""
import logging
import os
import secrets
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import bitrix, complaints, db, quality, stages, supervision
from .config import settings
from .security import (
    client_ip,
    create_access_token,
    get_admin,
    login_lock_seconds_left,
    register_failed_login,
    reset_login_attempts,
    verify_totp,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])


class AdminLoginIn(BaseModel):
    password: str
    totp: str = ""  # одноразовый код, если 2FA включена


class BlockIn(BaseModel):
    blocked: bool


class WhitelistIn(BaseModel):
    phone: str
    label: str = ""


class ComplaintIn(BaseModel):
    email: str
    category: str
    text: str


class ComplaintStatusIn(BaseModel):
    status: str
    resolution: str = ""


class RefusalReasonIn(BaseModel):
    reason: str
    comment: str = ""
    source: str = "qc"


# ---------------------------------------------------------------------------
# Вход
# ---------------------------------------------------------------------------
@router.get("/api/auth/config")
def auth_config():
    """Что нужно спросить на форме входа. Публичный эндпоинт: отдаёт только
    флаг «включена ли 2FA», сам секрет наружу не уходит."""
    return {"totp_required": settings.totp_enabled}


@router.post("/api/login")
def admin_login(body: AdminLoginIn, request: Request):
    ip = client_ip(request)

    left = login_lock_seconds_left(ip)
    if left:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Слишком много попыток. Попробуйте через {left} сек.")

    def _fail(reason: str, detail: str) -> HTTPException:
        # Неудачные входы пишем в лог сервиса (journalctl -u favorit-admin) —
        # схему БД это не трогает, а разбирать попытки подбора без следов
        # невозможно.
        register_failed_login(ip)
        log.warning("Неудачный вход в админку: %s, IP %s", reason, ip)
        if login_lock_seconds_left(ip):
            log.warning("Вход с IP %s заблокирован на %s сек",
                        ip, settings.admin_login_lockout_seconds)
        return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail)

    if body.password != settings.admin_password:
        raise _fail("неверный пароль", "Неверный пароль")

    if settings.totp_enabled:
        if not body.totp:
            # Пароль верный, но нужен второй фактор. Попыткой это не считаем:
            # тот, кто уже знает пароль, ничего не подбирает.
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Введите код из приложения-аутентификатора",
                headers={"X-Totp-Required": "1"})
        if not verify_totp(body.totp):
            # Неверный второй фактор считаем неудачной попыткой наравне с
            # паролем — иначе перебор шестизначного кода ничем не ограничен.
            raise _fail("неверный код 2FA", "Неверный или уже использованный код")

    reset_login_attempts(ip)
    log.info("Вход в админку с IP %s (%s)", ip,
             "2FA" if settings.totp_enabled else "только пароль")
    return {"access_token": create_access_token("admin", role="admin")}


# ---------------------------------------------------------------------------
# Сводка
# ---------------------------------------------------------------------------
@router.get("/api/stats")
def admin_stats(_: str = Depends(get_admin)):
    # bitrix_domain нужен frontend'у для построения ссылок из карточки
    # клиента в Bitrix (страница «Клиенты» + плитки дашборда). Раньше
    # домен был захардкожен в admin.html — сломался у портала с другим
    # доменом.
    data = dict(db.stats())
    data["bitrix_domain"] = settings.bitrix_domain
    return data


@router.get("/api/bitrix-link/{email}")
def admin_bitrix_link(email: str, _: str = Depends(get_admin)):
    """Прямые URL контакта и последней сделки клиента в Bitrix CRM.

    Frontend вызывает при клике на «↗ Открыть» в таблице клиентов.
    Возвращаем именно ID-based URL (не query-фильтр), потому что фильтры
    /crm/contact/list?FILTER_EMAIL= работают не на всех порталах и часто
    отдают 404.

    Кэш в bitrix.client_bitrix_urls хранится в памяти процесса — второй
    клик на того же клиента не идёт в Bitrix API.
    """
    return bitrix.client_bitrix_urls(email)


@router.get("/api/nps")
def admin_nps(
    kind: str = "",
    month: str = "",
    limit: int = 50,
    offset: int = 0,
    _: str = Depends(get_admin),
):
    """Оценки клиентов вместе с менеджером и задачей контроля качества.

    kind: '' | low (0-6) | neutral (7-8) | top (9-10).
    """
    return quality.list_scores(kind=kind, year_month=month,
                               limit=limit, offset=offset)


@router.get("/api/nps/trend")
def admin_nps_trend(months: int = 6, _: str = Depends(get_admin)):
    """Помесячный тренд. Пока основной backend не начнёт сохранять оценки
    0-6, это средняя по промоутерам, а не классический NPS."""
    return db.nps_trend(months)


# ---------------------------------------------------------------------------
# Контроль качества и рейтинг менеджеров
# ---------------------------------------------------------------------------
@router.get("/api/quality/status")
def quality_status(_: str = Depends(get_admin)):
    """Состояние механики: что не разобрано, что не отправлено, всё ли настроено."""
    return quality.status()


@router.get("/api/quality/rating")
def quality_rating(month: str = "", _: str = Depends(get_admin)):
    """Рейтинг менеджеров за месяц. Пусто → текущий месяц."""
    return quality.rating(month or quality.month_key())


@router.post("/api/quality/sync")
def quality_sync(_: str = Depends(get_admin)):
    """Разобрать новые оценки и поставить задачи прямо сейчас, не дожидаясь
    таймера. Нужно, когда контроль качества ждёт разбора конкретной жалобы."""
    return {
        "linked": quality.process_new_scores(),
        "tasks": quality.create_pending_tasks(),
    }


@router.post("/api/quality/report/{year_month}")
def quality_send_report(year_month: str, force: bool = False,
                        _: str = Depends(get_admin)):
    """Отправить месячный отчёт руководителям. Повторно — только с force=true."""
    if len(year_month) != 7 or year_month[4] != "-":
        raise HTTPException(status_code=400,
                            detail="Месяц указывается как YYYY-MM")
    return quality.send_monthly_report(year_month, force=force)


# ---------------------------------------------------------------------------
# Пользователи
# ---------------------------------------------------------------------------
@router.get("/api/users")
def admin_users(
    q: str = "",
    status_filter: str = "",
    sort: str = "last_seen",
    limit: int = 50,
    offset: int = 0,
    _: str = Depends(get_admin),
):
    return db.list_users(query=q, status=status_filter, sort=sort,
                         limit=limit, offset=offset)


@router.get("/api/users/{email}")
def admin_user(email: str, _: str = Depends(get_admin)):
    user = db.get_user(email)
    if not user:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    return user


@router.post("/api/users/{email}/block")
def admin_block(email: str, body: BlockIn, _: str = Depends(get_admin)):
    db.set_blocked(email, body.blocked)
    db.log_event(email, "blocked" if body.blocked else "unblocked")
    return {"ok": True, "blocked": body.blocked}


@router.post("/api/users/{email}/reset")
def admin_reset(email: str, _: str = Depends(get_admin)):
    db.reset_sessions(email)
    db.log_event(email, "sessions_reset")
    return {"ok": True}


@router.delete("/api/users/{email}")
def admin_delete(email: str, request: Request, _: str = Depends(get_admin)):
    """Полное удаление данных пользователя (152-ФЗ)."""
    # Пишем в лог сервиса ДО удаления: после него ни пользователя, ни его
    # событий в базе не останется, а необратимое удаление ПДн должно оставить
    # хоть какой-то след. journalctl -u favorit-admin.
    log.warning("Удаление данных пользователя %s (152-ФЗ), инициировано с IP %s",
                email, client_ip(request))
    db.delete_user(email)
    return {"ok": True}


# ---------------------------------------------------------------------------
# База коллекторов
#
# Раньше страница ходила за этими данными в основной backend (/collectors/*).
# Перенесли сюда: админ-сервис читает ту же базу напрямую, а фильтры
# реализуемы только на своей стороне.
# ---------------------------------------------------------------------------
@router.get("/api/collectors")
def admin_collectors(
    q: str = "",
    status_filter: str = "",
    category: str = "",
    sort: str = "reports",
    limit: int = 50,
    offset: int = 0,
    _: str = Depends(get_admin),
):
    return db.list_collectors(status=status_filter, query=q, category=category,
                              sort=sort, limit=limit, offset=offset)


@router.delete("/api/collectors/{phone}")
def admin_delete_collector(phone: str, _: str = Depends(get_admin)):
    db.delete_collector(phone)
    return {"ok": True}


@router.get("/api/collectors/whitelist")
def admin_whitelist(_: str = Depends(get_admin)):
    return db.list_whitelist()


@router.post("/api/collectors/whitelist")
def admin_add_whitelist(body: WhitelistIn, _: str = Depends(get_admin)):
    phone = db.norm_phone(body.phone)
    if len(phone) < 10:
        raise HTTPException(status_code=400, detail="Некорректный номер")
    db.add_whitelist(body.phone, body.label)
    return {"ok": True, "phone": phone}


@router.delete("/api/collectors/whitelist/{phone}")
def admin_delete_whitelist(phone: str, _: str = Depends(get_admin)):
    db.delete_whitelist(phone)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Стадии банкротства
# ---------------------------------------------------------------------------
@router.get("/api/stages")
def admin_stages(_: str = Depends(get_admin)):
    """Воронка, средний срок дела и состояние снимка — одним запросом:
    на дашборде они всегда показываются вместе."""
    return {"funnel": stages.funnel(), "overall": stages.overall(),
            "status": stages.status()}


@router.post("/api/stages/sync")
def admin_stages_sync(background: BackgroundTasks, force: bool = False,
                      _: str = Depends(get_admin)):
    """Запустить обновление снимка из Bitrix в фоне. force игнорирует
    интервал.

    Раньше был синхронным — при 2000 сделок sync занимал 60-120 сек и
    nginx (proxy_read_timeout 60s) обрывал соединение с 504 Gateway
    Timeout. Теперь endpoint отвечает мгновенно, а sync идёт фоном.
    Прогресс/готовность видно через GET /api/stages (поле status.last_sync
    обновляется когда фон закончит).
    """
    background.add_task(stages.sync, force=force)
    return {"status": "started",
            "note": "Sync запущен в фоне. Через 30-120 сек обновите /api/stages"}


# ---------------------------------------------------------------------------
# Отдел сопровождения: отказы и зависшие дела
# ---------------------------------------------------------------------------
@router.get("/api/supervision")
def admin_supervision(_: str = Depends(get_admin)):
    """Таблица менеджеров с долей отказов и состояние раздела — одним
    запросом: на странице они всегда показываются вместе."""
    return {"managers": supervision.manager_stats(),
            "status": supervision.status()}


@router.get("/api/supervision/refusals")
def admin_refusals(
    manager_id: int = 0,
    reason: str = "",
    unknown_only: bool = False,
    limit: int = 50,
    offset: int = 0,
    _: str = Depends(get_admin),
):
    return supervision.refusals(manager_id=manager_id, reason=reason,
                                unknown_only=unknown_only,
                                limit=limit, offset=offset)


@router.post("/api/supervision/refusals/{deal_id}")
def admin_set_refusal_reason(deal_id: int, body: RefusalReasonIn,
                             actor: str = Depends(get_admin)):
    try:
        return supervision.set_reason(deal_id, body.reason, body.comment,
                                      body.source, stated_by=actor)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/api/supervision/stuck")
def admin_stuck_deals(limit: int = 100, _: str = Depends(get_admin)):
    """Дела, стоящие на стадии дольше нормы этой стадии."""
    return supervision.stuck_deals(limit=limit)


# ---------------------------------------------------------------------------
# Жалобы
# ---------------------------------------------------------------------------
@router.get("/api/complaints")
def admin_complaints(
    status_filter: str = "",
    category: str = "",
    q: str = "",
    overdue: bool = False,
    limit: int = 50,
    offset: int = 0,
    _: str = Depends(get_admin),
):
    return complaints.list_complaints(
        status=status_filter, category=category, query=q,
        overdue_only=overdue, limit=limit, offset=offset)


@router.post("/api/complaints/{complaint_id}/status")
def admin_complaint_status(complaint_id: int, body: ComplaintStatusIn,
                           _: str = Depends(get_admin)):
    try:
        return complaints.set_status(complaint_id, body.status, body.resolution)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))


# ---------------------------------------------------------------------------
# Служебный эндпоинт для основного backend
#
# Жалобу подаёт клиент в приложении, то есть запись инициирует favorit-app.
# Он не пишет в таблицу напрямую: тогда правила подачи (одна открытая на
# тему, паузы, лимиты) пришлось бы продублировать там, и две копии политики
# неизбежно разъехались бы. Вместо этого он передаёт жалобу сюда, а все
# проверки остаются в одном месте — здесь.
#
# Nginx НЕ должен проксировать /admin/internal/* наружу, см. README.
# ---------------------------------------------------------------------------
def require_internal(request: Request) -> None:
    token = settings.internal_api_token.strip()
    if not token:
        # Fail closed: незаполненная настройка не должна означать открытый
        # приём жалоб от кого угодно.
        raise HTTPException(status_code=503,
                            detail="Служебный доступ не настроен")
    sent = request.headers.get("x-internal-token", "")
    # Сравниваем байты, а не строки: compare_digest на строках требует ASCII
    # и бросает исключение на чужом заголовке с кириллицей — вместо отказа
    # получался бы 500, то есть внешний запрос ронял бы обработчик.
    if not secrets.compare_digest(sent.encode("utf-8"), token.encode("utf-8")):
        log.warning("Служебный запрос с неверным токеном, IP %s",
                    client_ip(request))
        raise HTTPException(status_code=403, detail="Доступ запрещён")


@router.post("/internal/complaints", status_code=201)
def internal_submit_complaint(body: ComplaintIn, request: Request):
    require_internal(request)
    try:
        return complaints.submit(body.email, body.category, body.text)
    except complaints.Rejected as e:
        # 409, а не 400: запрос корректен, просто сейчас подавать нельзя.
        # Текст показывается клиенту в приложении как есть.
        raise HTTPException(status_code=409,
                            detail={"message": e.message, "reason": e.reason})


@router.get("/internal/complaints/categories")
def internal_categories(request: Request):
    """Список тем для формы в приложении — чтобы не хардкодить их дважды."""
    require_internal(request)
    return {"categories": [{"code": k, "label": v}
                           for k, v in complaints.CATEGORIES.items()],
            "answer_days": settings.complaint_answer_days,
            "text_min": settings.complaint_text_min,
            "text_max": settings.complaint_text_max}


@router.get("/api/chats")
def admin_chats_list(
    days: int = 30,
    only_waiting_hours: float = 0,
    filter_lawyer: str = "",
    filter_client: str = "",
    _: str = Depends(get_admin),
):
    """Лента всех чатов клиентов из мобильного приложения.
    Читается напрямую из общей `chat_messages` таблицы SQLite (та же БД
    что и основной backend). Одна строка на клиента: последнее сообщение,
    ФИО куратора, счётчики.

    Фильтры:
      · days — активность за N дней
      · only_waiting_hours — только те где клиент писал последним и
        прошло больше N часов без ответа юриста
      · filter_lawyer — подстрока в author_name
      · filter_client — подстрока в email или app_name/name клиента
    """
    from datetime import datetime, timezone, timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=int(days))).isoformat(timespec="seconds")

    with db._conn() as c:
        rows = c.execute(
            "SELECT email, MAX(id) AS last_id, COUNT(*) AS total_msgs "
            "FROM chat_messages WHERE created_at > ? "
            "GROUP BY email ORDER BY last_id DESC",
            (cutoff,),
        ).fetchall()

        clients = []
        now = datetime.now(timezone.utc)
        for r in rows:
            email = r["email"]
            last = c.execute(
                "SELECT created_at, incoming, kind, author_name, "
                "substr(text, 1, 120) txt, is_file, file_name "
                "FROM chat_messages WHERE id = ?",
                (r["last_id"],),
            ).fetchone()
            lawyer_row = c.execute(
                "SELECT author_name FROM chat_messages "
                "WHERE email = ? AND incoming = 1 AND author_name != '' "
                "ORDER BY id DESC LIMIT 1",
                (email,),
            ).fetchone()
            lawyer_name = lawyer_row["author_name"] if lawyer_row else ""
            # Колонка users.app_name — миграция из основного backend
            # (favorit_app), может ещё не примениться к общей БД. Пробуем
            # прочитать, при OperationalError падаем на просто name.
            client_name = ""
            try:
                user_row = c.execute(
                    "SELECT app_name, name FROM users WHERE email = ?",
                    (email,),
                ).fetchone()
                if user_row:
                    client_name = (user_row["app_name"] or user_row["name"] or "").strip()
            except Exception:
                user_row = c.execute(
                    "SELECT name FROM users WHERE email = ?",
                    (email,),
                ).fetchone()
                if user_row:
                    client_name = (user_row["name"] or "").strip()
            counts = c.execute(
                "SELECT SUM(CASE WHEN incoming=0 THEN 1 ELSE 0 END) client_n, "
                "SUM(CASE WHEN incoming=1 THEN 1 ELSE 0 END) lawyer_n "
                "FROM chat_messages WHERE email = ? AND created_at > ?",
                (email, cutoff),
            ).fetchone()

            last_from = "lawyer" if last["incoming"] else "client"
            try:
                last_dt = datetime.fromisoformat(last["created_at"].replace("Z", "+00:00"))
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=timezone.utc)
                hours_since = (now - last_dt).total_seconds() / 3600
            except Exception:
                hours_since = 0

            waiting = last_from == "client" and hours_since > 0
            preview = last["txt"] or ""
            if last["is_file"] and last["file_name"]:
                preview = f"📎 {last['file_name']}"
            if last["kind"] == "system" and not preview:
                preview = "(системное сообщение)"

            item = {
                "email": email,
                "client_name": client_name,
                "lawyer_name": lawyer_name,
                "last_at": last["created_at"],
                "last_from": last_from,
                "last_text": preview,
                "hours_since_last": round(hours_since, 1),
                "waiting": waiting,
                "client_msgs": int(counts["client_n"] or 0),
                "lawyer_msgs": int(counts["lawyer_n"] or 0),
                "total_msgs": int(r["total_msgs"] or 0),
            }
            if only_waiting_hours > 0:
                if not waiting or hours_since < only_waiting_hours:
                    continue
            if filter_lawyer and filter_lawyer.lower() not in lawyer_name.lower():
                continue
            if filter_client:
                fc = filter_client.lower()
                if fc not in email.lower() and fc not in client_name.lower():
                    continue
            clients.append(item)

    summary = {
        "clients_total": len(clients),
        "waiting_2h": sum(1 for x in clients if x["waiting"] and x["hours_since_last"] >= 2),
        "waiting_4h": sum(1 for x in clients if x["waiting"] and x["hours_since_last"] >= 4),
        "answered_last": sum(1 for x in clients if x["last_from"] == "lawyer"),
    }
    return {"summary": summary, "clients": clients}


@router.get("/api/chats/{email}")
def admin_chat_history(email: str, limit: int = 300, _: str = Depends(get_admin)):
    """Полная переписка с одним клиентом — раскрывается в модалке."""
    with db._conn() as c:
        rows = c.execute(
            "SELECT id, created_at, incoming, kind, author_name, text, "
            "is_file, file_url, file_name FROM chat_messages "
            "WHERE email = ? ORDER BY id DESC LIMIT ?",
            (email.lower(), int(limit)),
        ).fetchall()
    messages = [
        {
            "id": r["id"],
            "at": r["created_at"],
            "from": "lawyer" if r["incoming"] else "client",
            "author": r["author_name"] or "",
            "text": r["text"] or "",
            "kind": r["kind"] or "msg",
            "is_file": bool(r["is_file"]),
            "file_url": r["file_url"] or "",
            "file_name": r["file_name"] or "",
        }
        for r in rows
    ]
    return {"email": email, "messages": list(reversed(messages))}


@router.get("")
def admin_page():
    path = os.path.join(os.path.dirname(__file__), "static", "admin.html")
    return FileResponse(path)
