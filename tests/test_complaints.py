"""Жалобы клиентов: правила подачи, задачи, статусы, служебный приём.

Политика и её обоснование — docs/research-complaints.md.
"""
import pytest

from app import complaints
from app.config import settings

TEXT = "Менеджер не выходит на связь третью неделю, дело стоит на месте."


@pytest.fixture(autouse=True)
def _no_cooldown(monkeypatch):
    """Пауза между подачами проверяется отдельным тестом; в остальных она
    только мешала бы видеть работу главного правила."""
    monkeypatch.setattr(settings, "complaint_cooldown_seconds", 0)


class TestВалидация:
    @pytest.mark.parametrize("category,text,reason", [
        ("нет-такой", TEXT, "bad_category"),
        ("manager", "коротко", "text_too_short"),
        ("manager", "я" * 5000, "text_too_long"),
    ])
    def test_плохой_ввод_отвергается(self, category, text, reason):
        with pytest.raises(complaints.Rejected) as e:
            complaints.submit("a@ex.ru", category, text)
        assert e.value.reason == reason

    def test_без_адреса_не_принимаем(self):
        with pytest.raises(complaints.Rejected) as e:
            complaints.submit("", "manager", TEXT)
        assert e.value.reason == "no_email"


class TestГлавноеПравило:
    def test_повтор_по_теме_отклонён(self):
        complaints.submit("a@ex.ru", "manager", TEXT)
        with pytest.raises(complaints.Rejected) as e:
            complaints.submit("a@ex.ru", "manager", TEXT)
        assert e.value.reason == "duplicate_category"

    def test_отказ_написан_для_клиента(self):
        complaints.submit("a@ex.ru", "manager", TEXT)
        with pytest.raises(complaints.Rejected) as e:
            complaints.submit("a@ex.ru", "manager", TEXT)
        msg = e.value.message
        assert "ответим до" in msg, "клиент должен увидеть срок ответа"
        assert "лимит" not in msg.lower(), "это не про лимит, а про уже поданную"
        assert "duplicate" not in msg, "машинный код не для клиента"

    def test_другая_тема_принимается(self):
        complaints.submit("a@ex.ru", "manager", TEXT)
        assert complaints.submit("a@ex.ru", "money", TEXT)["id"] > 0

    def test_чужой_клиент_не_задет(self):
        complaints.submit("a@ex.ru", "manager", TEXT)
        assert complaints.submit("b@ex.ru", "manager", TEXT)["id"] > 0

    @pytest.mark.parametrize("closing", ["resolved", "rejected"])
    def test_закрытие_освобождает_тему(self, closing):
        first = complaints.submit("a@ex.ru", "manager", TEXT)
        complaints.set_status(first["id"], closing, "разобрались")
        assert complaints.submit("a@ex.ru", "manager", TEXT)["id"] > 0


class TestПредохранители:
    def test_пауза_между_подачами(self, monkeypatch):
        monkeypatch.setattr(settings, "complaint_cooldown_seconds", 300)
        complaints.submit("p@ex.ru", "manager", TEXT)
        with pytest.raises(complaints.Rejected) as e:
            complaints.submit("p@ex.ru", "app", TEXT)
        assert e.value.reason == "cooldown"
        assert "мин" in e.value.message

    def test_суточный_предел(self, monkeypatch):
        monkeypatch.setattr(settings, "complaint_max_per_day", 3)
        for cat in ("manager", "money", "app"):
            complaints.submit("d@ex.ru", cat, TEXT)
        with pytest.raises(complaints.Rejected) as e:
            complaints.submit("d@ex.ru", "deadlines", TEXT)
        assert e.value.reason == "day_limit"

    def test_месячный_предел(self, monkeypatch):
        monkeypatch.setattr(settings, "complaint_max_per_day", 100)
        monkeypatch.setattr(settings, "complaint_max_per_month", 2)
        complaints.submit("m@ex.ru", "manager", TEXT)
        complaints.submit("m@ex.ru", "money", TEXT)
        with pytest.raises(complaints.Rejected) as e:
            complaints.submit("m@ex.ru", "app", TEXT)
        assert e.value.reason == "month_limit"


class TestЗадачи:
    def test_жалоба_становится_задачей(self, bitrix):
        bitrix.managers["a@ex.ru"] = (101, "Анна Смирнова")
        complaints.submit("a@ex.ru", "manager", TEXT)
        assert complaints.process_pending()["created"] == 1
        task = bitrix.tasks[0]
        assert task["to"] == settings.bitrix_qc_head_id
        assert TEXT[:20] in task["body"]
        assert "защите прав потребителей" in task["body"]
        assert "Анна Смирнова" in task["body"]

    def test_дедлайн_короче_срока_ответа(self, bitrix):
        from datetime import datetime, timezone
        complaints.submit("a@ex.ru", "manager", TEXT)
        complaints.process_pending()
        deadline = datetime.fromisoformat(bitrix.tasks[0]["deadline"])
        days = (deadline - datetime.now(timezone.utc)).days
        assert days < settings.complaint_answer_days
        assert deadline.weekday() < 5, "дедлайн выпал на выходной"

    def test_повторный_прогон_не_дублирует(self, bitrix):
        complaints.submit("a@ex.ru", "manager", TEXT)
        complaints.process_pending()
        complaints.process_pending()
        assert len(bitrix.tasks) == 1

    def test_сбой_битрикса_не_теряет_жалобу(self, bitrix):
        from app.bitrix import BitrixError
        complaints.submit("a@ex.ru", "manager", TEXT)
        bitrix.fail_tasks = BitrixError("500 Internal")
        complaints.process_pending()
        row = complaints.list_complaints()["items"][0]
        assert row["attempts"] == 1 and "500" in row["last_error"]


class TestСрокиИСтатусы:
    def test_срок_ответа_десять_дней(self):
        assert complaints.submit("a@ex.ru", "manager", TEXT)["answer_days"] == 10

    def test_просрочка_видна(self, fresh_db):
        import sqlite3
        from datetime import datetime, timedelta, timezone
        c = complaints.submit("a@ex.ru", "manager", TEXT)
        past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(
            timespec="seconds")
        with sqlite3.connect(str(fresh_db)) as x:
            x.execute("UPDATE complaints SET answer_due = ? WHERE id = ?",
                      (past, c["id"]))
        assert complaints.stats()["overdue"] == 1
        assert complaints.list_complaints(overdue_only=True)["total"] == 1

    def test_закрытая_не_считается_просроченной(self, fresh_db):
        import sqlite3
        from datetime import datetime, timedelta, timezone
        c = complaints.submit("a@ex.ru", "manager", TEXT)
        past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(
            timespec="seconds")
        with sqlite3.connect(str(fresh_db)) as x:
            x.execute("UPDATE complaints SET answer_due = ? WHERE id = ?",
                      (past, c["id"]))
        complaints.set_status(c["id"], "resolved", "ответили")
        assert complaints.stats()["overdue"] == 0

    def test_неизвестный_статус_отвергается(self):
        c = complaints.submit("a@ex.ru", "manager", TEXT)
        with pytest.raises(ValueError):
            complaints.set_status(c["id"], "выдумка")

    def test_несуществующая_жалоба(self):
        with pytest.raises(LookupError):
            complaints.set_status(999999, "resolved")


class TestСлужебныйПриём:
    HEAD = {"X-Internal-Token": "test-internal-token"}

    def test_с_токеном_принимается(self, client):
        r = client.post("/admin/internal/complaints", headers=self.HEAD,
                        json={"email": "n@ex.ru", "category": "app", "text": TEXT})
        assert r.status_code == 201 and "answer_due" in r.json()

    def test_без_токена_отказ(self, client):
        assert client.post("/admin/internal/complaints",
                           json={"email": "n@ex.ru", "category": "app",
                                 "text": TEXT}).status_code == 403

    def test_повтор_по_теме_даёт_409_с_текстом_для_клиента(self, client):
        client.post("/admin/internal/complaints", headers=self.HEAD,
                    json={"email": "n@ex.ru", "category": "app", "text": TEXT})
        r = client.post("/admin/internal/complaints", headers=self.HEAD,
                        json={"email": "n@ex.ru", "category": "app", "text": TEXT})
        assert r.status_code == 409
        assert "ответим до" in r.json()["detail"]["message"]

    def test_темы_отдаются_приложению(self, client):
        r = client.get("/admin/internal/complaints/categories", headers=self.HEAD)
        assert len(r.json()["categories"]) == len(complaints.CATEGORIES)

    def test_без_настроенного_токена_эндпоинт_закрыт(self, client, monkeypatch):
        """Незаполненная настройка не должна означать открытый приём."""
        monkeypatch.setattr(settings, "internal_api_token", "")
        assert client.post("/admin/internal/complaints", headers=self.HEAD,
                           json={"email": "n@ex.ru", "category": "app",
                                 "text": TEXT}).status_code == 503


class TestВОтчёте:
    def test_жалобы_попадают_в_месячный_отчёт(self, bitrix):
        from app import quality
        complaints.submit("a@ex.ru", "manager", TEXT)
        quality.send_monthly_report(quality.month_key())
        body = bitrix.tasks[0]["body"]
        assert "Жалобы" in body and "Работа менеджера" in body
