#!/usr/bin/env python3
"""Разбор оценок клиентов: задачи контроля качества и месячный отчёт.

Запускается systemd-таймером каждые 10 минут (см. favorit-nps.timer).
Один прогон делает три вещи и завершается:

  1. привязывает новые оценки к менеджеру (ответственный из Битрикса);
  2. ставит задачи по оценкам 0-6 на отдел контроля качества;
  3. первого числа отправляет отчёт за прошлый месяц руководителям.

Всё идемпотентно: повторный запуск не создаёт вторых задач и не шлёт
отчёт дважды. Поэтому таймер можно дёргать сколь угодно часто.

Вручную:
    .venv/bin/python scripts/nps_worker.py
    .venv/bin/python scripts/nps_worker.py --report 2026-07 --force
"""
import argparse
import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import complaints, db, quality, stages, supervision  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description="Воркер оценок клиентов")
    p.add_argument("--report", metavar="YYYY-MM",
                   help="отправить отчёт за конкретный месяц и выйти")
    p.add_argument("--force", action="store_true",
                   help="отправить отчёт повторно, даже если он уже уходил")
    p.add_argument("--dry-run", action="store_true",
                   help="только показать состояние, ничего не делать")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Все таблицы, к которым воркер обращается. Раньше поднималась только
    # схема quality, и на свежей машине воркер падал с «no such table»:
    # таймер может сработать раньше, чем веб-сервис поднимется хоть раз.
    db.init()
    quality.init()
    complaints.init()
    stages.init()
    supervision.init()

    if args.dry_run:
        print(json.dumps(quality.status(), ensure_ascii=False, indent=2))
        return 0

    if args.report:
        result = quality.send_monthly_report(args.report, force=args.force)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 1

    result = quality.run_worker()
    print(json.dumps(result, ensure_ascii=False, indent=2))

    # Ненулевой код только если отчёт был должен уйти и не ушёл — systemd
    # покажет юнит как failed, и это будет видно в мониторинге. Недоступный
    # Битрикс при разборе оценок не в счёт: следующий прогон через 10 минут.
    report = result.get("report") or {}
    return 0 if report.get("skipped") or report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
