"""Стадии банкротства: синхронизация, воронка, сроки, методика.

Данные повторяют реальную воронку 15 «БФЛ. Сопровождение».
"""
from datetime import datetime, timedelta, timezone

import pytest

from app import stages
from app.config import settings

NOW = datetime.now(timezone.utc)


def ago(days: float) -> str:
    return (NOW - timedelta(days=days)).isoformat(timespec="seconds")


STAGE_LIST = [
    ("C15:NEW", "тех.этап", 10),
    ("C15:FINAL_INVOICE", "Договор заключен", 20),
    ("C15:UC_3T0KG4", "Сбор документов", 30),
    ("C15:UC_CLJIUB", "Переданы на подачу (юристы)", 40),
    ("C15:UC_T28ZLJ", "Поданы", 50),
    ("C15:UC_7ICUJ1", "Оставлено без движения", 60),
    ("C15:UC_50J5B9", "Заявление принято, СЗ назначено", 70),
    ("C15:PREPARATION", "Заседание отложено", 80),
    ("C15:UC_LWCW5Y", "Реструктуризация", 90),
    ("C15:UC_3UFLUY", "Реализация", 100),
    ("C15:UC_HN6K2D", "Клиенты от Гараджи", 110),
    ("C15:UC_ZTR9AW", "Пауза", 120),
    ("C15:UC_UJI8T9", "Долг списан", 130),
    ("C15:PREPAYMENT_INVOIC", "Условный отказ", 140),
    ("C15:EXECUTING", "Отказ от работы", 150),
    ("C15:WON", "Успешно реализовано", 160),
    ("C15:LOSE", "Сделка провалена", 170),
]

# Три завершённых дела (300, 400 и 200 дней), одно в работе, одно провалено,
# одно на паузе.
DEALS = [
    dict(ID=1, STAGE_ID="C15:WON", CONTACT_ID=11, ASSIGNED_BY_ID=101,
         DATE_CREATE=ago(320), DATE_MODIFY=ago(10), CLOSEDATE=ago(15)),
    dict(ID=2, STAGE_ID="C15:UC_UJI8T9", CONTACT_ID=12, ASSIGNED_BY_ID=101,
         DATE_CREATE=ago(420), DATE_MODIFY=ago(20), CLOSEDATE=None),
    dict(ID=3, STAGE_ID="C15:UC_UJI8T9", CONTACT_ID=13, ASSIGNED_BY_ID=102,
         DATE_CREATE=ago(210), DATE_MODIFY=ago(10), CLOSEDATE=None),
    dict(ID=4, STAGE_ID="C15:UC_T28ZLJ", CONTACT_ID=14, ASSIGNED_BY_ID=102,
         DATE_CREATE=ago(100), DATE_MODIFY=ago(5), CLOSEDATE=None),
    dict(ID=5, STAGE_ID="C15:LOSE", CONTACT_ID=15, ASSIGNED_BY_ID=101,
         DATE_CREATE=ago(150), DATE_MODIFY=ago(30), CLOSEDATE=ago(30)),
    dict(ID=6, STAGE_ID="C15:UC_ZTR9AW", CONTACT_ID=16, ASSIGNED_BY_ID=102,
         DATE_CREATE=ago(260), DATE_MODIFY=ago(2), CLOSEDATE=None),
]


def h(deal, stage, days):
    return dict(OWNER_ID=deal, STAGE_ID=stage, CREATED_TIME=ago(days))


HISTORY = [
    h(1, "C15:FINAL_INVOICE", 320), h(1, "C15:UC_3T0KG4", 310),
    h(1, "C15:UC_T28ZLJ", 290), h(1, "C15:UC_3UFLUY", 200),
    h(1, "C15:UC_UJI8T9", 20), h(1, "C15:WON", 15),
    h(2, "C15:FINAL_INVOICE", 420), h(2, "C15:UC_3T0KG4", 400),
    h(2, "C15:UC_T28ZLJ", 380), h(2, "C15:UC_LWCW5Y", 300),
    h(2, "C15:UC_UJI8T9", 20),
    h(3, "C15:FINAL_INVOICE", 210), h(3, "C15:UC_3T0KG4", 205),
    h(3, "C15:UC_T28ZLJ", 190), h(3, "C15:UC_3UFLUY", 120),
    h(3, "C15:UC_UJI8T9", 10),
    h(4, "C15:FINAL_INVOICE", 100), h(4, "C15:UC_3T0KG4", 90),
    h(4, "C15:UC_T28ZLJ", 60),
    h(5, "C15:FINAL_INVOICE", 150), h(5, "C15:UC_3T0KG4", 140),
    h(5, "C15:LOSE", 30),
    h(6, "C15:FINAL_INVOICE", 260), h(6, "C15:UC_3T0KG4", 250),
    h(6, "C15:UC_ZTR9AW", 240),
]


@pytest.fixture
def funnel_data(monkeypatch):
    from app import bitrix
    monkeypatch.setattr(bitrix, "configured", lambda: True)
    monkeypatch.setattr(bitrix, "deal_stages", lambda cat: [
        dict(stage_id=s, name=n, sort=o, semantics="") for s, n, o in STAGE_LIST])
    monkeypatch.setattr(bitrix, "deals",
                        lambda cat, modified_since="", limit=2000: DEALS)
    monkeypatch.setattr(bitrix, "stage_history",
                        lambda cat, since="", limit=20000: HISTORY)
    stages.sync(force=True)


@pytest.mark.parametrize("stage_id,kind", [
    ("C15:UC_UJI8T9", "done"),
    ("C15:WON", "done"),            # после списания дело уже не «в работе»
    ("C15:NEW", "service"),
    ("C15:UC_HN6K2D", "service"),   # признак источника, не этап
    ("C15:UC_ZTR9AW", "stuck"),
    ("C15:PREPARATION", "stuck"),
    ("C15:UC_7ICUJ1", "stuck"),
    ("C15:LOSE", "failed"),
    ("C15:EXECUTING", "failed"),
    ("C15:PREPAYMENT_INVOIC", "failed"),
    ("C15:UC_3T0KG4", "work"),
])
def test_классификация_стадий(stage_id, kind):
    assert stages.stage_kind(stage_id) == kind


class TestСинхронизация:
    def test_снимок_загружается(self, funnel_data):
        st = stages.status()
        assert st["stages"] == len(STAGE_LIST)
        assert st["deals"] == len(DEALS)
        assert st["history_rows"] == len(HISTORY)

    def test_повтор_отбрасывается_по_интервалу(self, funnel_data):
        assert "skipped" in stages.sync()

    def test_повтор_не_дублирует_историю(self, funnel_data):
        stages.sync(force=True)
        assert stages.status()["history_rows"] == len(HISTORY)

    def test_без_битрикса_пропускается(self, no_bitrix):
        assert "skipped" in stages.sync(force=True)

    def test_сбой_битрикса_не_роняет_метрики(self, funnel_data, monkeypatch):
        from app import bitrix
        def boom(*a, **k):
            raise bitrix.BitrixError("сеть недоступна")
        monkeypatch.setattr(bitrix, "deal_stages", boom)
        assert "error" in stages.sync(force=True)
        assert stages.overall()["done"]["count"] == 3, "старый снимок потерян"


class TestСреднийСрокДела:
    def test_считается_по_дошедшим_до_списания(self, funnel_data):
        done = stages.overall()["done"]
        assert done["count"] == 3
        assert 299 <= done["avg_days"] <= 301        # (300+400+200)/3
        assert 299 <= done["median_days"] <= 301
        assert 399 <= done["max_days"] <= 401

    def test_незавершённые_не_занижают_среднее(self, funnel_data):
        ov = stages.overall()
        assert ov["done"]["count"] < ov["total_deals"]

    def test_в_работе_и_провалы_разведены(self, funnel_data):
        ov = stages.overall()
        assert ov["in_progress"] == 2      # дела 4 и 6
        assert ov["failed"] == 1           # дело 5

    def test_на_пустой_базе_не_падает(self):
        assert stages.overall()["done"]["avg_days"] is None


class TestВоронка:
    def test_служебные_стадии_скрыты(self, funnel_data):
        ids = [s["stage_id"] for s in stages.funnel()["stages"]]
        assert "C15:NEW" not in ids and "C15:UC_HN6K2D" not in ids

    def test_порядок_как_в_битриксе(self, funnel_data):
        rows = stages.funnel()["stages"]
        assert [s["sort"] for s in rows] == sorted(s["sort"] for s in rows)

    def test_количество_на_стадии(self, funnel_data):
        by = {s["stage_id"]: s for s in stages.funnel()["stages"]}
        assert by["C15:UC_T28ZLJ"]["current"] == 1
        assert by["C15:UC_ZTR9AW"]["current"] == 1

    def test_альтернативные_процедуры_помечены(self, funnel_data):
        by = {s["stage_id"]: s for s in stages.funnel()["stages"]}
        assert by["C15:UC_LWCW5Y"]["alternative"] is True
        assert by["C15:UC_3UFLUY"]["alternative"] is True
        assert by["C15:UC_3T0KG4"]["alternative"] is False


class TestСрокНаСтадии:
    def test_считается_по_завершённым_прохождениям(self, funnel_data):
        by = {s["stage_id"]: s for s in stages.funnel()["stages"]}
        # На «Поданы» сейчас дело 4 — его прохождение ещё не закончилось.
        assert by["C15:UC_T28ZLJ"]["count"] == 3
        assert by["C15:UC_T28ZLJ"]["current"] == 1

    def test_медиана_устойчивее_среднего(self, funnel_data):
        by = {s["stage_id"]: s for s in stages.funnel()["stages"]}
        sbor = by["C15:UC_3T0KG4"]
        assert sbor["median_days"] < sbor["avg_days"], (
            "выброс должен тянуть среднее вверх, а медиану — нет")

    def test_мало_данных_вместо_цифры(self, funnel_data):
        by = {s["stage_id"]: s for s in stages.funnel()["stages"]}
        assert by["C15:UC_LWCW5Y"]["count"] == 1
        assert by["C15:UC_LWCW5Y"]["enough"] is False

    def test_дольше_всех_показано(self, funnel_data):
        by = {s["stage_id"]: s for s in stages.funnel()["stages"]}
        assert by["C15:UC_ZTR9AW"]["longest_wait_days"] > 200


def test_через_api(client, admin, funnel_data):
    r = client.get("/admin/api/stages", headers=admin)
    assert r.status_code == 200
    data = r.json()
    assert {"funnel", "overall", "status"} <= set(data)
    assert data["status"]["min_sample"] == stages.MIN_SAMPLE
    assert data["overall"]["done_stage_name"] == "Долг списан"


def test_стадии_на_пустой_базе_через_api(client, admin):
    assert client.get("/admin/api/stages", headers=admin).status_code == 200
