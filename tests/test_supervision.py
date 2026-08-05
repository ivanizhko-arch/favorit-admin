"""Отдел сопровождения: отказы по менеджерам и перехват зависших дел."""
import pytest

from app import stages, supervision
from app.config import settings
from tests.test_stages import HISTORY, STAGE_LIST, ago


# Менеджер 101: 4 дела, 1 отказ  → 25 %
# Менеджер 102: 2 дела, 1 отказ  → 50 %  (меньше дел, но хуже доля)
DEALS = [
    dict(ID=1, STAGE_ID="C15:WON", CONTACT_ID=11, ASSIGNED_BY_ID=101,
         DATE_CREATE=ago(320), DATE_MODIFY=ago(10), CLOSEDATE=ago(15)),
    dict(ID=2, STAGE_ID="C15:UC_UJI8T9", CONTACT_ID=12, ASSIGNED_BY_ID=101,
         DATE_CREATE=ago(420), DATE_MODIFY=ago(20), CLOSEDATE=None),
    dict(ID=3, STAGE_ID="C15:UC_T28ZLJ", CONTACT_ID=13, ASSIGNED_BY_ID=101,
         DATE_CREATE=ago(100), DATE_MODIFY=ago(5), CLOSEDATE=None),
    dict(ID=4, STAGE_ID="C15:LOSE", CONTACT_ID=14, ASSIGNED_BY_ID=101,
         DATE_CREATE=ago(150), DATE_MODIFY=ago(30), CLOSEDATE=ago(30)),
    dict(ID=5, STAGE_ID="C15:UC_3T0KG4", CONTACT_ID=15, ASSIGNED_BY_ID=102,
         DATE_CREATE=ago(260), DATE_MODIFY=ago(2), CLOSEDATE=None),
    dict(ID=6, STAGE_ID="C15:EXECUTING", CONTACT_ID=16, ASSIGNED_BY_ID=102,
         DATE_CREATE=ago(90), DATE_MODIFY=ago(40), CLOSEDATE=ago(40)),
]

# Дело 5 стоит на «Сборе документов» очень долго — оно и есть зависшее.
EXTRA_HISTORY = HISTORY + [
    dict(OWNER_ID=4, STAGE_ID="C15:LOSE", CREATED_TIME=ago(30)),
    dict(OWNER_ID=5, STAGE_ID="C15:FINAL_INVOICE", CREATED_TIME=ago(260)),
    dict(OWNER_ID=5, STAGE_ID="C15:UC_3T0KG4", CREATED_TIME=ago(250)),
    dict(OWNER_ID=6, STAGE_ID="C15:FINAL_INVOICE", CREATED_TIME=ago(90)),
    dict(OWNER_ID=6, STAGE_ID="C15:EXECUTING", CREATED_TIME=ago(40)),
]


@pytest.fixture
def funnel(monkeypatch, bitrix):
    from app import bitrix as real
    monkeypatch.setattr(real, "deal_stages", lambda cat: [
        dict(stage_id=s, name=n, sort=o, semantics="") for s, n, o in STAGE_LIST])
    monkeypatch.setattr(real, "deals",
                        lambda cat, modified_since="", limit=2000: DEALS)
    monkeypatch.setattr(real, "stage_history",
                        lambda cat, since="", limit=20000: EXTRA_HISTORY)
    monkeypatch.setattr(real, "user_name",
                        lambda uid: {101: "Анна Смирнова",
                                     102: "Борис Козлов"}.get(uid, ""))
    stages.sync(force=True)
    return bitrix


class TestОтказыПоМенеджерам:
    def test_доля_важнее_количества(self, funnel):
        by = {m["manager_id"]: m
              for m in supervision.manager_stats()["managers"]}
        assert by[101]["deals"] == 4 and by[101]["refusals"] == 1
        assert by[101]["refusal_rate"] == 25.0
        assert by[102]["deals"] == 2 and by[102]["refusals"] == 1
        assert by[102]["refusal_rate"] == 50.0

    def test_сначала_показываем_худших_по_доле(self, funnel):
        items = supervision.manager_stats()["managers"]
        assert items[0]["manager_id"] == 102, (
            "менеджер с меньшим числом отказов, но худшей долей должен быть выше")

    def test_имена_подставлены_из_кэша(self, funnel):
        by = {m["manager_id"]: m
              for m in supervision.manager_stats()["managers"]}
        assert by[101]["manager_name"] == "Анна Смирнова"

    def test_итоги_по_отделу(self, funnel):
        tot = supervision.manager_stats()["totals"]
        assert tot["deals"] == 6 and tot["refusals"] == 2
        assert tot["refusal_rate"] == pytest.approx(33.3, abs=0.1)

    def test_причина_не_выяснена_считается(self, funnel):
        assert supervision.manager_stats()["totals"]["reason_unknown"] == 2


class TestПричины:
    def test_причина_записывается_с_источником(self, funnel):
        supervision.set_reason(4, "manager", "Не выходил на связь", "client")
        row = [r for r in supervision.refusals()["items"] if r["deal_id"] == 4][0]
        assert row["reason"] == "manager"
        assert row["reason_label"] == "Не устроил менеджер"
        assert row["source_label"] == "со слов клиента"
        assert row["manager_fault"] is True

    def test_только_вина_менеджера_идёт_в_отдельную_колонку(self, funnel):
        supervision.set_reason(4, "manager", source="client")   # его вина
        supervision.set_reason(6, "price", source="client")     # не его
        by = {m["manager_id"]: m
              for m in supervision.manager_stats()["managers"]}
        assert by[101]["manager_fault"] == 1
        assert by[102]["manager_fault"] == 0, (
            "цена и обстоятельства клиента не должны вешаться на менеджера")

    def test_неизвестная_причина_отвергается(self, funnel):
        with pytest.raises(ValueError):
            supervision.set_reason(4, "выдумка")

    def test_неизвестный_источник_отвергается(self, funnel):
        with pytest.raises(ValueError):
            supervision.set_reason(4, "manager", source="сосед")

    def test_фильтр_только_без_причины(self, funnel):
        supervision.set_reason(4, "price", source="qc")
        left = supervision.refusals(unknown_only=True)
        assert left["total"] == 1 and left["items"][0]["deal_id"] == 6

    def test_фильтр_по_менеджеру(self, funnel):
        assert supervision.refusals(manager_id=102)["total"] == 1

    def test_сколько_прожило_дело(self, funnel):
        row = [r for r in supervision.refusals()["items"] if r["deal_id"] == 4][0]
        assert 118 <= row["days_lived"] <= 122     # 150 - 30

    def test_задача_на_выяснение_причины(self, funnel):
        r = supervision.ask_refusal_reasons()
        assert r["created"] == 2
        assert all(t["to"] == settings.bitrix_qc_head_id for t in funnel.tasks)
        assert "причину отказа" in funnel.tasks[0]["title"]

    def test_повторный_прогон_не_дублирует_задачи(self, funnel):
        supervision.ask_refusal_reasons()
        before = len(funnel.tasks)
        supervision.ask_refusal_reasons()
        assert len(funnel.tasks) == before

    def test_по_выясненной_причине_задача_не_ставится(self, funnel):
        supervision.set_reason(4, "price", source="qc")
        supervision.set_reason(6, "competitor", source="qc")
        assert supervision.ask_refusal_reasons()["created"] == 0


class TestЗависшиеДела:
    def test_дело_на_стадии_дольше_нормы_попадает_в_риск(self, funnel):
        ids = [d["deal_id"] for d in supervision.stuck_deals()["items"]]
        assert 5 in ids, "дело, стоящее 250 дней на сборе документов, не найдено"

    def test_завершённые_и_отказные_не_считаются_зависшими(self, funnel):
        ids = [d["deal_id"] for d in supervision.stuck_deals()["items"]]
        for done_or_failed in (1, 2, 4, 6):
            assert done_or_failed not in ids

    def test_у_каждой_стадии_своя_норма(self, funnel):
        th = supervision.stage_thresholds()
        # «Сбор документов» проходят за дни, «Реализация» — за месяцы.
        assert th["C15:UC_3T0KG4"] < th["C15:UC_3UFLUY"]

    def test_норма_не_ниже_минимума(self, funnel, monkeypatch):
        monkeypatch.setattr(settings, "stuck_min_days", 30)
        assert all(v >= 30 for v in supervision.stage_thresholds().values())

    def test_без_данных_берётся_порог_по_умолчанию(self, funnel, monkeypatch):
        monkeypatch.setattr(settings, "stuck_default_days", 99)
        monkeypatch.setattr(stages, "stage_medians", lambda: {})
        assert set(supervision.stage_thresholds().values()) == {99.0}

    def test_перехват_ставит_задачу_руководителю_сопровождения(self, funnel):
        r = supervision.alert_stuck_deals()
        assert r["created"] >= 1
        assert funnel.tasks[0]["to"] == settings.bitrix_support_head_id
        assert "не двигается" in funnel.tasks[0]["body"]

    def test_повторно_по_той_же_стадии_не_предупреждаем(self, funnel):
        supervision.alert_stuck_deals()
        before = len(funnel.tasks)
        supervision.alert_stuck_deals()
        assert len(funnel.tasks) == before, (
            "дело стоит на той же стадии — это не повод для второй задачи")

    def test_после_предупреждения_дело_помечено(self, funnel):
        supervision.alert_stuck_deals()
        stuck = supervision.stuck_deals()["items"]
        assert any(d["alerted"] for d in stuck)


class TestЧерезAPI:
    def test_таблица_менеджеров(self, client, admin, funnel):
        r = client.get("/admin/api/supervision", headers=admin)
        assert r.status_code == 200
        assert r.json()["managers"]["totals"]["deals"] == 6

    def test_список_отказов(self, client, admin, funnel):
        assert client.get("/admin/api/supervision/refusals",
                          headers=admin).json()["total"] == 2

    def test_указание_причины(self, client, admin, funnel):
        r = client.post("/admin/api/supervision/refusals/4", headers=admin,
                        json={"reason": "manager", "source": "client",
                              "comment": "не брал трубку"})
        assert r.status_code == 200

    def test_кривая_причина_отвергается(self, client, admin, funnel):
        assert client.post("/admin/api/supervision/refusals/4", headers=admin,
                           json={"reason": "ерунда"}).status_code == 400

    def test_зависшие_через_api(self, client, admin, funnel):
        assert client.get("/admin/api/supervision/stuck",
                          headers=admin).status_code == 200

    def test_нужна_авторизация(self, client):
        for path in ("/admin/api/supervision", "/admin/api/supervision/refusals",
                     "/admin/api/supervision/stuck"):
            assert client.get(path).status_code == 401, path


def test_на_пустой_базе_не_падает():
    assert supervision.manager_stats()["managers"] == []
    assert supervision.stuck_deals()["total"] == 0
    assert supervision.status()["refusals"] == 0
