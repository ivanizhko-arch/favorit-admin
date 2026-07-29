"""Админ-панель приложения: API для управления пользователями + сама страница."""
import logging
import os
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import db, quality
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
    return db.stats()


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


@router.get("")
def admin_page():
    path = os.path.join(os.path.dirname(__file__), "static", "admin.html")
    return FileResponse(path)
