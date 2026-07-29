import base64
import hashlib
import hmac
import secrets
import struct
import time
from typing import Optional
import jwt
from fastapi import Depends, HTTPException, Request, status
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


# ---------------------------------------------------------------------------
# TOTP (RFC 6238) — второй фактор для входа в админку.
#
# Реализовано на stdlib (hmac + hashlib), без сторонних библиотек: алгоритм
# занимает 20 строк, а лишняя зависимость в проде — лишний риск обновления.
# Совместимо с Google Authenticator, Яндекс.Ключ, 1Password, Authy.
# ---------------------------------------------------------------------------
_TOTP_STEP = 30       # длина окна в секундах (стандарт)
_TOTP_DIGITS = 6
_TOTP_WINDOW = 1      # ±1 шаг: терпим расхождение часов телефона до 30 сек

# Уже использованные счётчики — один и тот же код нельзя предъявить дважды
# (защита от повтора перехваченного кода в течение его 30-секундной жизни).
_used_totp_counters: set[int] = set()


def generate_totp_secret(length: int = 20) -> str:
    """Новый случайный base32-секрет (20 байт = 160 бит, как рекомендует RFC)."""
    return base64.b32encode(secrets.token_bytes(length)).decode().rstrip("=")


def totp_uri(secret: str, account: str, issuer: str) -> str:
    """otpauth-ссылка для QR-кода в приложении-аутентификаторе."""
    from urllib.parse import quote
    label = quote(f"{issuer}:{account}")
    return (f"otpauth://totp/{label}?secret={secret}"
            f"&issuer={quote(issuer)}&digits={_TOTP_DIGITS}&period={_TOTP_STEP}")


def _b32decode(secret: str) -> bytes:
    """Base32 без учёта регистра, пробелов и с восстановлением padding.
    Пользователи копируют секрет из мессенджера — там бывает и то, и другое."""
    s = secret.strip().replace(" ", "").replace("-", "").upper()
    s += "=" * (-len(s) % 8)
    return base64.b32decode(s, casefold=True)


def _totp_at(key: bytes, counter: int) -> str:
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(code % (10 ** _TOTP_DIGITS)).zfill(_TOTP_DIGITS)


def verify_totp(code: str, secret: Optional[str] = None) -> bool:
    """Проверить одноразовый код. False — если код неверен, просрочен или
    уже был использован."""
    secret = secret if secret is not None else settings.admin_totp_secret
    code = (code or "").strip().replace(" ", "")
    if not secret or not code.isdigit() or len(code) != _TOTP_DIGITS:
        return False
    try:
        key = _b32decode(secret)
    except Exception:
        # Секрет в .env битый — считаем 2FA непройденной, а не «отключённой».
        return False

    now_counter = int(time.time()) // _TOTP_STEP
    for shift in range(-_TOTP_WINDOW, _TOTP_WINDOW + 1):
        counter = now_counter + shift
        if hmac.compare_digest(_totp_at(key, counter), code):
            if counter in _used_totp_counters:
                return False  # код уже предъявляли — повтор не принимаем
            _used_totp_counters.add(counter)
            # Чистим протухшие счётчики, чтобы set не рос бесконечно.
            _used_totp_counters.difference_update(
                {c for c in _used_totp_counters if c < now_counter - _TOTP_WINDOW}
            )
            return True
    return False


# ---------------------------------------------------------------------------
# Ограничение попыток входа.
#
# Хранится в памяти процесса: сервис однопроцессный (systemd + один uvicorn),
# а перезапуск сбрасывает счётчики — это приемлемо, т.к. цель не абсолютная
# защита, а замедление перебора пароля до бессмысленной скорости.
# ---------------------------------------------------------------------------
_login_attempts: dict[str, tuple[int, float]] = {}  # ip -> (счётчик, время последней)


def client_ip(request: Request) -> str:
    """IP клиента с учётом того, что мы стоим за Nginx."""
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "?"


def login_lock_seconds_left(ip: str) -> int:
    """Сколько секунд осталось до разблокировки. 0 — вход разрешён."""
    count, last = _login_attempts.get(ip, (0, 0.0))
    if count < settings.admin_login_max_attempts:
        return 0
    left = int(settings.admin_login_lockout_seconds - (time.time() - last))
    if left <= 0:
        _login_attempts.pop(ip, None)  # срок вышел — начинаем считать заново
        return 0
    return left


def register_failed_login(ip: str) -> None:
    count, _ = _login_attempts.get(ip, (0, 0.0))
    _login_attempts[ip] = (count + 1, time.time())


def reset_login_attempts(ip: str) -> None:
    _login_attempts.pop(ip, None)
