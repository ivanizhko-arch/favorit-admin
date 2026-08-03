"""Поиск, фильтры и постраничный вывод в клиентах и базе коллекторов."""
import pytest

from app import db


@pytest.fixture
def users(fresh_db):
    db.upsert_login("anna@ex.ru")
    db.touch_consent("anna@ex.ru")
    db.upsert_login("boris@ex.ru")
    db.set_blocked("boris@ex.ru", True)
    db.upsert_login("100_500@ex.ru")     # служебные символы в LIKE
    db.upsert_login("<script>@ex.ru")    # разметка в данных


@pytest.fixture
def collectors(fresh_db):
    for _ in range(3):
        db.report_collector("+7 (495) 123-45-67", "collector", "9990001122")
    db.report_collector("+7 926 000-00-01", "scammer", "9990001122")


class TestКлиенты:
    def test_поиск_по_части_адреса(self, users):
        assert db.list_users(query="anna")["total"] == 1

    def test_фильтр_по_статусу(self, users):
        assert db.list_users(status="blocked")["total"] == 1
        assert db.list_users(status="active")["total"] == 3

    def test_фильтр_по_согласию(self, users):
        assert db.list_users(status="consented")["total"] == 1
        assert db.list_users(status="no_consent")["total"] == 3

    @pytest.mark.parametrize("query", ["%", "_", "100_500", "a_n"])
    def test_служебные_символы_поиска_экранированы(self, users, query):
        """Без экранирования поиск по одиночному проценту возвращал всю базу."""
        found = db.list_users(query=query)["total"]
        assert found <= 1, f"«{query}» вернул {found} записей"

    def test_подстрока_с_подчёркиванием_находит_точно(self, users):
        assert db.list_users(query="100_500")["total"] == 1

    def test_постраничный_вывод(self, users):
        page = db.list_users(limit=2, offset=0)
        assert len(page["items"]) == 2 and page["total"] == 4
        assert len(db.list_users(limit=2, offset=2)["items"]) == 2

    def test_мусор_в_сортировке_не_ломает_запрос(self, users):
        """Колонка подставляется в SQL строкой, поэтому идёт через белый список."""
        assert db.list_users(sort="; DROP TABLE users--")["total"] == 4

    def test_предел_страницы_ограничен(self, users):
        assert db.list_users(limit=10 ** 6)["limit"] <= 500


class TestКоллекторы:
    def test_порог_жалоб_переводит_в_подтверждённые(self, collectors):
        confirmed = db.list_collectors(status="confirmed")
        assert confirmed["total"] == 1

    def test_фильтр_по_категории(self, collectors):
        assert db.list_collectors(category="scammer")["total"] == 1

    @pytest.mark.parametrize("query", [
        "+7 (495) 123-45-67",   # как показывает телефон
        "8 (495) 123-45-67",    # как диктуют по-русски
        "8 495 1234567",
        "84951234567",
        "74951234567",
        "4951234567",           # как лежит в базе
        "495 123",              # часть номера
    ])
    def test_поиск_терпим_к_формату_номера(self, collectors, query):
        """Администратор копирует номер откуда придётся. Восьмёрка и +7 дают
        11 цифр, а в базе номер хранится десятью — без приведения такой поиск
        не находил ничего."""
        assert db.list_collectors(query=query)["total"] == 1, query

    def test_несуществующий_номер_не_находится(self, collectors):
        assert db.list_collectors(query="5555555555")["total"] == 0

    def test_белый_список_снимает_блокировку(self, collectors):
        db.add_whitelist("+7 (495) 123-45-67", "Наш номер")
        assert db.list_collectors(status="confirmed")["total"] == 0
        assert db.lookup_number("4951234567")["safe"] is True


class TestЧерезAPI:
    def test_список_клиентов(self, client, admin, users):
        r = client.get("/admin/api/users?status_filter=blocked", headers=admin)
        assert r.status_code == 200 and r.json()["total"] == 1

    def test_сводка_содержит_прежние_ключи(self, client, admin, users):
        """На них опирается страница и возможные внешние вызовы."""
        s = client.get("/admin/api/stats", headers=admin).json()
        assert {"total", "blocked", "active_7d", "logins"} <= set(s)

    def test_удаление_клиента(self, client, admin, users):
        assert client.delete("/admin/api/users/boris@ex.ru",
                             headers=admin).status_code == 200
        assert db.list_users(query="boris")["total"] == 0
