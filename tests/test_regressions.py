"""Дефекты, которые уже случались. Каждый тест — след одного бага.

Если такой тест покраснел, значит вернулась известная поломка: в докстроке
написано, как она проявлялась и почему это важно. Это дешевле, чем искать
причину заново.

Все семь найдены аудитом 2026-07-31 и не ловились прежними проверками:
те гоняли один процесс, по одному запросу, на чистой базе.
"""
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app import complaints, quality, stages, store
from app.config import settings

REPO = Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.regression

TEXT = "Менеджер не выходит на связь третью неделю, дело стоит на месте."


def test_воркер_поднимает_все_схемы_на_свежей_базе(tmp_path):
    """Воркер поднимал только схему quality, а обращается ещё к общим
    таблицам, жалобам и стадиям. На новой машине systemd-таймер срабатывает
    раньше, чем веб-сервис поднимется хоть раз, и воркер умирал с
    «no such table: nps_scores» — то есть автоматика не заводилась вовсе.
    """
    import os

    env = {
        **os.environ,
        "DB_PATH": str(tmp_path / "fresh.db"),
        "BITRIX_WEBHOOK_URL": "",
        "ADMIN_TOTP_SECRET": "",
        # Воркер печатает по-русски; без этого на Windows вывод не декодируется.
        "PYTHONIOENCODING": "utf-8",
    }
    r = subprocess.run([sys.executable, "scripts/nps_worker.py"],
                       cwd=REPO, env=env, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    assert "no such table" not in (r.stderr or ""), r.stderr[-500:]
    assert r.returncode == 0, (
        "на ненастроенном сервере воркер не должен возвращать ошибку, иначе "
        f"systemd пометит юнит упавшим каждые 10 минут:\n{r.stdout[-400:]}")


def test_две_жалобы_по_одной_теме_не_создаются(bitrix):
    """Правило «одна открытая жалоба на тему» держалось только на проверке
    в коде. Проверка и вставка идут разными транзакциями, между ними окно:
    два быстрых нажатия создавали две открытые жалобы по одной теме — ровно
    в обход правила, ради которого всё и делалось.
    """
    # Оба «запроса» успели пройти проверку до того, как хоть один вставил.
    complaints.check_can_submit("race@ex.ru", "manager")
    complaints.check_can_submit("race@ex.ru", "manager")

    complaints.submit("race@ex.ru", "manager", TEXT)
    with pytest.raises(complaints.Rejected) as e:
        complaints.submit("race@ex.ru", "manager", TEXT)
    assert e.value.reason == "duplicate_category"

    open_now = complaints.list_complaints(status="open")["total"]
    assert open_now == 1, "в базе осталось больше одной открытой жалобы по теме"


def test_база_запрещает_дубль_даже_в_обход_кода(fresh_db):
    """Тот же дефект со стороны схемы: правило должно быть в базе, а не
    только в коде, иначе любая другая точка записи его обойдёт."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    row = ("dup@ex.ru", "money", TEXT, now, now, now)
    sql = ("INSERT INTO complaints (email, category, text, status, answer_due,"
           " created_at, updated_at) VALUES (?,?,?,'open',?,?,?)")
    with sqlite3.connect(str(fresh_db)) as c:
        c.execute(sql, row)
    with pytest.raises(sqlite3.IntegrityError):
        with sqlite3.connect(str(fresh_db)) as c:
            c.execute(sql, row)


@pytest.mark.parametrize("token,label", [
    ("токен".encode("utf-8"), "кириллица в байтах"),
    (bytes([0xC0, 0xFF, 0x80]), "байты старших кодов"),
    (b"", "пустой"),
])
def test_чужой_служебный_токен_даёт_отказ_а_не_ошибку(client, token, label):
    """compare_digest на строках требует ASCII. Заголовок с байтами старших
    кодов давал 500 вместо 403: внешний запрос вызывал ошибку сервера.
    """
    r = client.post("/admin/internal/complaints",
                    headers={"X-Internal-Token": token},
                    json={"email": "x@ex.ru", "category": "app", "text": TEXT})
    assert r.status_code == 403, f"{label}: получен {r.status_code}"


def test_база_в_режиме_wal_и_ждёт_блокировку():
    """favorit.db пишут три процесса: админка, воркер и основной backend.
    С журналом «delete» и таймаутом 5 секунд параллельная запись отваливалась
    с «database is locked» — на проде это выглядело бы случайными ошибками.
    """
    c = store.connect()
    try:
        assert str(c.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal"
        assert int(c.execute("PRAGMA busy_timeout").fetchone()[0]) >= 5000
    finally:
        c.close()


def test_запись_дожидается_чужой_транзакции(fresh_db):
    """Проверка того же с другой стороны: пока воркер держит транзакцию,
    запрос в админку должен подождать и записать, а не упасть."""
    holder = store.connect()
    holder.execute("BEGIN IMMEDIATE")
    holder.execute(
        "INSERT INTO complaints (email, category, text, status, answer_due,"
        " created_at, updated_at) VALUES ('hold@ex.ru','other',?,'open','x','x','x')",
        (TEXT,))

    result = {}

    def web_write():
        try:
            w = store.connect()
            w.execute(
                "INSERT INTO complaints (email, category, text, status, answer_due,"
                " created_at, updated_at) VALUES ('web@ex.ru','other',?,'open','x','x','x')",
                (TEXT,))
            w.commit()
            w.close()
            result["ok"] = True
        except sqlite3.OperationalError as e:      # pragma: no cover
            result["err"] = str(e)

    t = threading.Thread(target=web_write)
    t.start()
    time.sleep(0.7)              # воркер дописывает свою порцию
    holder.rollback()
    holder.close()
    t.join(timeout=20)
    assert result.get("ok"), result.get("err", "поток не завершился")


def test_обновление_стадий_пишет_порциями():
    """До 2000 сделок и 20000 записей истории писались одной транзакцией —
    всё это время админка и основной сервис не могли записать ничего."""
    src = (REPO / "app" / "stages.py").read_text(encoding="utf-8")
    assert "for chunk in _chunks(rows)" in src
    assert "for chunk in _chunks(hist)" in src
    assert stages.WRITE_CHUNK <= 500


@pytest.mark.parametrize("score", [42, -3, 11, 100])
def test_оценка_вне_шкалы_не_идёт_в_рейтинг(score, add_score, bitrix):
    """Основной сервис диапазон не проверяет. Запись со значением 42
    считалась отличной и улучшала итог менеджера, отрицательная порождала
    задачу контролю качества.
    """
    assert quality.category(score) == "invalid"

    bitrix.managers["weird@ex.ru"] = (101, "Анна Смирнова")
    add_score("weird@ex.ru", score)
    quality.process_new_scores()

    rating = quality.rating(quality.month_key())
    assert rating["totals"]["invalid"] == 1, "битая запись должна быть видна"
    assert rating["totals"]["top"] == 0
    assert rating["totals"]["low"] == 0
    assert rating["leader"] is None, "битая запись сделала кого-то лидером"

    assert quality.create_pending_tasks()["created"] == 0, (
        "по оценке вне шкалы не должно ставиться задачи контролю качества")


def test_ненастроенный_битрикс_это_пропуск_а_не_сбой(no_bitrix, monkeypatch):
    """Пока Битрикс не настроен, месячный отчёт возвращал ошибку, и systemd
    помечал юнит упавшим каждые 10 минут. За таким алертом перестают следить,
    и настоящий сбой пройдёт незамеченным.
    """
    monkeypatch.setattr(settings, "bitrix_qc_head_id", 0)
    monkeypatch.setattr(settings, "bitrix_support_head_id", 0)
    monkeypatch.setattr(settings, "bitrix_ceo_id", 0)
    result = quality.send_monthly_report(quality.previous_month_key())
    assert result.get("skipped"), result
    assert not result.get("error"), "незаконченная настройка — не сбой отправки"
