"""Проверки страницы админки. Сам набор — tests/ui/check_ui.js.

Он написан на JS, потому что проверяет код страницы: подсовывает заглушки
DOM и ответов сервера и смотрит, что нарисовалось. Переписывать это на
Python значило бы моделировать браузер, а не проверять реальный скрипт.

Если node не установлен, тест пропускается: разработчик backend не обязан
держать его у себя, а в CI node есть.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

CHECK = Path(__file__).resolve().parent / "ui" / "check_ui.js"


@pytest.mark.skipif(shutil.which("node") is None,
                    reason="node не установлен — проверки страницы пропущены")
def test_страница_админки():
    r = subprocess.run(["node", str(CHECK)], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    assert r.returncode == 0, "\n" + (r.stdout or "") + (r.stderr or "")
