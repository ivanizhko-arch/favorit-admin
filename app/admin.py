"""Админ-панель приложения: API для управления пользователями + сама страница."""
import os
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import db
from .config import settings
from .security import create_access_token, get_admin

router = APIRouter(prefix="/admin", tags=["admin"])


class AdminLoginIn(BaseModel):
    password: str


class BlockIn(BaseModel):
    blocked: bool


@router.post("/api/login")
def admin_login(body: AdminLoginIn):
    if body.password != settings.admin_password:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Неверный пароль")
    return {"access_token": create_access_token("admin", role="admin")}


@router.get("/api/stats")
def admin_stats(_: str = Depends(get_admin)):
    return db.stats()


@router.get("/api/users")
def admin_users(q: str = "", _: str = Depends(get_admin)):
    return db.list_users(q)


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
def admin_delete(email: str, _: str = Depends(get_admin)):
    """Полное удаление данных пользователя (152-ФЗ)."""
    db.delete_user(email)
    return {"ok": True}


@router.get("/api/nps")
def admin_nps(_: str = Depends(get_admin)):
    """Список NPS-оценок клиентов (сохраняются только промоутеры 8-10)."""
    return db.list_nps_scores()


@router.get("")
def admin_page():
    path = os.path.join(os.path.dirname(__file__), "static", "admin.html")
    return FileResponse(path)
