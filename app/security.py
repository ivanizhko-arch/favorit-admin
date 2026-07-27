import time
from typing import Optional
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from .config import settings

_bearer = HTTPBearer(auto_error=False)


def create_access_token(subject: str, role: str = "user") -> str:
    """Выдать JWT (subject = e-mail пользователя или 'admin')."""
    now = int(time.time())
    payload = {
        "sub": subject,
        "role": role,
        "iat": now,
        "exp": now + settings.access_token_ttl_minutes * 60,
    }
    return jwt.encode(payload, settings.secret_key, algorithm="HS256")


def _decode(creds: Optional[HTTPAuthorizationCredentials]) -> dict:
    if creds is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Требуется авторизация")
    try:
        return jwt.decode(creds.credentials, settings.secret_key, algorithms=["HS256"])
    except jwt.PyJWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Недействительный или истёкший токен")


def get_current_user(
    creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
) -> str:
    """Зависимость: проверяет токен, блокировку и актуальность сессии."""
    from . import db  # локальный импорт, чтобы избежать циклов
    payload = _decode(creds)
    email = payload["sub"]

    if db.is_blocked(email):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="Доступ заблокирован")
    if int(payload.get("iat", 0)) < db.sessions_valid_after(email):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Сессия завершена, войдите заново")
    return email


def get_admin(
    creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
) -> str:
    """Зависимость для админ-эндпоинтов."""
    payload = _decode(creds)
    if payload.get("role") != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="Нужны права администратора")
    return payload["sub"]
