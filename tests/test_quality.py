"""Контроль качества по оценкам и рейтинг менеджеров."""
import pytest

from app import quality
from app.config import settings


@pytest.mark.parametrize("score,expected", [
    (0, "low"), (5, "low"), (7, "low"),       # семёрка отнесена к низким
    (8, "neutral"),                            # единственная нейтральная
    (9, "top"), (10, "top"),
])
def test_градация(score, expected):
    assert quality.category(score) == expected


def test_подписи_диапазонов_строятся_из_настроек():
    """Раньше «0-6» было зашито в разметку в шести местах и разъезжалось
    с настройкой при первой же смене порога."""
    g = quality.grades()
    assert g["low_label"] == "0-7"
    assert g["neutral_label"] == "8"      # одно значение — без диапазона
    assert g["top_label"] == "9-10"
    assert g["has_neutral"] is True


def test_без_нейтральных_подпись_не_врёт(monkeypatch):
    monkeypatch.setattr(settings, "qc_promoter_min", 8)
    assert quality.grades()["has_neutral"] is False


class TestЗадачиКонтроляКачества:
    def test_низкая_оценка_создаёт_задачу(self, add_score, bitrix):
        bitrix.managers["sad@ex.ru"] = (102, "Борис Козлов")
        add_score("sad@ex.ru", 3)
        quality.process_new_scores()
        assert quality.create_pending_tasks()["created"] == 1
        task = bitrix.tasks[0]
        assert task["to"] == settings.bitrix_qc_head_id
        assert "3/10" in task["title"]
        assert "Борис Козлов" in task["body"]

    def test_семёрка_тоже_создаёт_задачу(self, add_score, bitrix):
        add_score("seven@ex.ru", 7)
        quality.process_new_scores()
        assert quality.create_pending_tasks()["created"] == 1

    def test_по_хорошей_оценке_задачи_нет(self, add_score, bitrix):
        add_score("glad@ex.ru", 10)
        quality.process_new_scores()
        assert quality.create_pending_tasks()["created"] == 0

    def test_повторный_прогон_не_дублирует(self, add_score, bitrix):
        add_score("sad@ex.ru", 2)
        quality.process_new_scores()
        quality.create_pending_tasks()
        quality.create_pending_tasks()
        assert len(bitrix.tasks) == 1

    def test_старая_оценка_не_поднимает_задачу(self, add_score, bitrix):
        """Иначе первый прогон на накопленной базе завалил бы отдел делами
        годичной давности."""
        add_score("old@ex.ru", 2, days_ago=40)
        quality.process_new_scores()
        assert quality.create_pending_tasks()["created"] == 0

    def test_сбой_битрикса_не_теряет_оценку(self, add_score, bitrix):
        from app.bitrix import BitrixError
        add_score("sad@ex.ru", 1)
        quality.process_new_scores()
        bitrix.fail_tasks = BitrixError("500 Internal")
        quality.create_pending_tasks()
        row = quality.list_scores(kind="low")["items"][0]
        assert row["attempts"] == 1 and "500" in row["last_error"]

    def test_сеть_легла_оценка_разберётся_позже(self, add_score, bitrix):
        from app.bitrix import BitrixError
        add_score("later@ex.ru", 4)
        bitrix.fail_resolve = BitrixError("сеть недоступна (timeout)")
        assert quality.process_new_scores()["linked"] == 0
        bitrix.fail_resolve = None
        assert quality.process_new_scores()["linked"] == 1


class TestРейтинг:
    def test_итог_это_лучшие_минус_низкие(self, add_score, bitrix):
        bitrix.managers.update({"a@ex.ru": (101, "Анна"), "b@ex.ru": (102, "Борис")})
        for _ in range(2):
            add_score("a@ex.ru", 10)
        add_score("a@ex.ru", 3)
        add_score("b@ex.ru", 9)
        quality.process_new_scores()

        by = {m["manager_id"]: m for m in
              quality.rating(quality.month_key())["managers"]}
        assert by[101]["top"] == 2 and by[101]["low"] == 1 and by[101]["net"] == 1
        assert by[102]["net"] == 1

    def test_качество_побеждает_объём(self, add_score, bitrix):
        """Ради этого и отказались от суммы баллов."""
        bitrix.managers.update({"vol@ex.ru": (201, "Объёмный"),
                                "acc@ex.ru": (202, "Аккуратный")})
        for _ in range(5):
            add_score("vol@ex.ru", 10)
        for _ in range(4):
            add_score("vol@ex.ru", 1)
        for _ in range(2):
            add_score("acc@ex.ru", 9)
        quality.process_new_scores()

        leader = quality.rating(quality.month_key())["leader"]
        assert leader["manager_id"] == 202, "победил объём, а не качество"

    def test_нейтральные_не_влияют(self, add_score, bitrix):
        bitrix.managers["a@ex.ru"] = (101, "Анна")
        add_score("a@ex.ru", 8)
        quality.process_new_scores()
        r = quality.rating(quality.month_key())
        assert r["totals"]["neutral"] == 1
        assert r["leader"] is None, "нейтральная оценка сделала лидера"

    def test_неопределённый_менеджер_не_становится_лидером(self, add_score, bitrix):
        add_score("nobody@ex.ru", 10)
        quality.process_new_scores()
        r = quality.rating(quality.month_key())
        assert any(m["manager_id"] == 0 for m in r["managers"])
        assert r["leader"] is None, "«Не определён» — дырка в данных, не сотрудник"

    def test_пустой_месяц_не_падает(self):
        assert quality.rating("2019-01")["leader"] is None


class TestМесячныйОтчёт:
    def test_уходит_трём_руководителям(self, add_score, bitrix):
        bitrix.managers["a@ex.ru"] = (101, "Анна")
        add_score("a@ex.ru", 10)
        quality.process_new_scores()
        r = quality.send_monthly_report(quality.month_key())
        assert r["ok"] and len(r["task_ids"]) == 3
        assert [t["to"] for t in bitrix.tasks] == [501, 502, 503]

    def test_повторно_не_отправляется(self, bitrix):
        ym = quality.month_key()
        quality.send_monthly_report(ym)
        bitrix.tasks.clear()
        assert quality.send_monthly_report(ym)["already_sent"] is True
        assert not bitrix.tasks

    def test_принудительно_отправляется_снова(self, bitrix):
        ym = quality.month_key()
        quality.send_monthly_report(ym)
        bitrix.tasks.clear()
        assert quality.send_monthly_report(ym, force=True)["ok"]
        assert len(bitrix.tasks) == 3

    def test_в_отчёте_объяснено_правило(self, bitrix):
        quality.send_monthly_report(quality.month_key())
        body = bitrix.tasks[0]["body"]
        assert "минус количество" in body
        assert "сумма оценок" not in body

    def test_первого_числа_отчёт_за_прошлый_месяц(self):
        from datetime import datetime, timezone
        assert quality.due_report_month(
            datetime(2026, 10, 1, tzinfo=timezone.utc)) == "2026-09"

    def test_январь_отсылает_к_декабрю(self):
        from datetime import datetime, timezone
        assert quality.previous_month_key(
            datetime(2026, 1, 9, tzinfo=timezone.utc)) == "2025-12"
