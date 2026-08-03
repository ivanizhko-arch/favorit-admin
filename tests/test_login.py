"""Вход в админку: пароль, второй фактор, защита от подбора."""
import time

import pytest

from app import security
from app.config import settings


def test_верный_пароль_даёт_токен(client):
    r = client.post("/admin/api/login", json={"password": "test-password"})
    assert r.status_code == 200
    assert r.json()["access_token"]


def test_неверный_пароль_отвергается(client):
    assert client.post("/admin/api/login",
                       json={"password": "wrong"}).status_code == 401


def test_без_токена_данные_недоступны(client):
    for path in ("/admin/api/stats", "/admin/api/users", "/admin/api/complaints",
                 "/admin/api/quality/status", "/admin/api/stages"):
        assert client.get(path).status_code == 401, path


def test_после_пяти_неудач_вход_блокируется(client):
    codes = [client.post("/admin/api/login", json={"password": "x"}).status_code
             for _ in range(settings.admin_login_max_attempts + 1)]
    assert codes[-1] == 429, codes
    # Даже верный пароль теперь не проходит — иначе блокировка бесполезна.
    assert client.post("/admin/api/login",
                       json={"password": "test-password"}).status_code == 429


def test_успешный_вход_сбрасывает_счётчик(client):
    client.post("/admin/api/login", json={"password": "x"})
    client.post("/admin/api/login", json={"password": "test-password"})
    codes = [client.post("/admin/api/login", json={"password": "x"}).status_code
             for _ in range(settings.admin_login_max_attempts)]
    assert 429 not in codes, "счётчик не обнулился после удачного входа"


class TestДвухфакторная:
    @pytest.fixture(autouse=True)
    def _totp(self, monkeypatch):
        self.secret = security.generate_totp_secret()
        monkeypatch.setattr(settings, "admin_totp_secret", self.secret)

    def code(self, shift: int = 0) -> str:
        counter = int(time.time()) // 30 + shift
        return security._totp_at(security._b32decode(self.secret), counter)

    def test_страница_знает_что_нужен_код(self, client):
        assert client.get("/admin/api/auth/config").json()["totp_required"] is True

    def test_пароль_без_кода_не_пускает(self, client):
        r = client.post("/admin/api/login", json={"password": "test-password"})
        assert r.status_code == 401
        assert r.headers.get("X-Totp-Required") == "1"

    def test_верный_код_пускает(self, client):
        r = client.post("/admin/api/login",
                        json={"password": "test-password", "totp": self.code()})
        assert r.status_code == 200, r.text

    def test_неверный_код_не_пускает(self, client):
        assert client.post("/admin/api/login",
                           json={"password": "test-password",
                                 "totp": "000000"}).status_code == 401

    def test_код_нельзя_предъявить_дважды(self, client):
        code = self.code()
        assert client.post("/admin/api/login",
                           json={"password": "test-password",
                                 "totp": code}).status_code == 200
        assert client.post("/admin/api/login",
                           json={"password": "test-password",
                                 "totp": code}).status_code == 401

    def test_код_из_соседнего_окна_принимается(self, client):
        """Часы на телефоне расходятся с сервером — это норма."""
        assert client.post("/admin/api/login",
                           json={"password": "test-password",
                                 "totp": self.code(-1)}).status_code == 200

    def test_код_из_далёкого_окна_не_принимается(self, client):
        assert client.post("/admin/api/login",
                           json={"password": "test-password",
                                 "totp": self.code(-10)}).status_code == 401

    def test_верный_код_не_спасает_при_неверном_пароле(self, client):
        assert client.post("/admin/api/login",
                           json={"password": "wrong",
                                 "totp": self.code()}).status_code == 401

    def test_битый_секрет_закрывает_вход(self, client, monkeypatch):
        """Испорченный секрет в .env не должен означать «второго фактора нет»."""
        monkeypatch.setattr(settings, "admin_totp_secret", "не-base32-!!!")
        assert client.post("/admin/api/login",
                           json={"password": "test-password",
                                 "totp": "123456"}).status_code == 401
