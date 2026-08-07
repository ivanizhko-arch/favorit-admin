"""Общая обвязка тестов.

Переменные окружения выставляются ДО импорта приложения: `app.main` при
импорте поднимает схему, и без этого тесты писали бы в боевую базу из `.env`.
"""
import os
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# Значения по умолчанию для тестов. setdefault, а не присваивание: так можно
# прогнать тесты против другой базы, задав DB_PATH снаружи.
_BOOT = tempfile.mkdtemp(prefix="favorit-admin-tests-")
os.environ.setdefault("DB_PATH", os.path.join(_BOOT, "boot.db"))
os.environ.setdefault("ADMIN_PASSWORD", "test-password")
os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("ADMIN_TOTP_SECRET", "")
os.environ.setdefault("BITRIX_WEBHOOK_URL", "")
os.environ.setdefault("INTERNAL_API_TOKEN", "test-internal-token")

from fastapi.testclient import TestClient  # noqa: E402

from app import (complaints, db, quality, security, stages,  # noqa: E402
                 store, supervision)
from app.config import settings  # noqa: E402
from app.main import app  # noqa: E402

ADMIN_PASSWORD = os.environ["DB_PATH"] and "test-password"
INTERNAL_TOKEN = "test-internal-token"


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    """Своя пустая база на каждый тест — тесты не должны видеть чужие данные."""
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "favorit.db"))
    store._wal_ready = False
    db.init()
    quality.init()
    complaints.init()
    stages.init()
    supervision.init()
    yield tmp_path / "favorit.db"


@pytest.fixture(autouse=True)
def reset_process_state():
    """Счётчики входа и использованные коды 2FA живут в памяти процесса.
    Без сброса тесты влияли бы друг на друга через них."""
    security._login_attempts.clear()
    security._used_totp_counters.clear()
    yield
    security._login_attempts.clear()
    security._used_totp_counters.clear()


@pytest.fixture(autouse=True)
def default_settings(monkeypatch):
    """Настройки, от которых отталкиваются тесты. Отдельные тесты меняют их
    через monkeypatch — он же вернёт исходные значения после теста."""
    monkeypatch.setattr(settings, "admin_password", "test-password")
    monkeypatch.setattr(settings, "admin_totp_secret", "")
    monkeypatch.setattr(settings, "internal_api_token", INTERNAL_TOKEN)
    monkeypatch.setattr(settings, "qc_detractor_max", 7)
    monkeypatch.setattr(settings, "qc_promoter_min", 9)
    monkeypatch.setattr(settings, "bitrix_qc_head_id", 501)
    monkeypatch.setattr(settings, "bitrix_support_head_id", 502)
    monkeypatch.setattr(settings, "bitrix_ceo_id", 503)
    yield


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def admin(client):
    """Заголовки авторизованного администратора."""
    r = client.post("/admin/api/login", json={"password": "test-password"})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["access_token"]}


class BitrixStub:
    """Подменяет Битрикс: сеть в тестах не трогаем, а вызовы видно."""

    def __init__(self):
        self.tasks = []
        self.managers = {}
        self.fail_tasks = None      # исключение, если надо сымитировать сбой
        self.fail_resolve = None

    def create_task(self, title, body, responsible_id, deadline=""):
        if self.fail_tasks:
            raise self.fail_tasks
        self.tasks.append({"title": title, "body": body,
                           "to": responsible_id, "deadline": deadline})
        return 9000 + len(self.tasks)

    def resolve_manager(self, email):
        if self.fail_resolve:
            raise self.fail_resolve
        return self.managers.get(email, (0, ""))


@pytest.fixture
def bitrix(monkeypatch):
    from app import bitrix as real

    stub = BitrixStub()
    monkeypatch.setattr(real, "configured", lambda: True)
    monkeypatch.setattr(real, "create_task", stub.create_task)
    monkeypatch.setattr(real, "resolve_manager", stub.resolve_manager)
    return stub


@pytest.fixture
def no_bitrix(monkeypatch):
    from app import bitrix as real
    monkeypatch.setattr(real, "configured", lambda: False)


@pytest.fixture
def add_score(fresh_db):
    """Записать оценку так, как это делает основной сервис."""
    import sqlite3
    from datetime import datetime, timedelta, timezone

    def _add(email: str, score: int, days_ago: float = 0.0) -> int:
        at = (datetime.now(timezone.utc) - timedelta(days=days_ago))
        with sqlite3.connect(str(fresh_db)) as c:
            cur = c.execute(
                "INSERT INTO nps_scores (email, score, created_at) VALUES (?,?,?)",
                (email, score, at.isoformat(timespec="seconds")))
            return int(cur.lastrowid)

    return _add
