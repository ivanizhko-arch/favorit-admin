"""Минимальный клиент Битрикс24 для админ-сервиса.

Основной backend ходит в Битрикс своим кодом; сюда он не подключён, а
контроль качества обязан создавать задачи сам. Поэтому здесь ровно то, что
нужно этому сервису: вызов метода, поиск ответственного за клиента и
постановка задачи.

Работаем через входящий вебхук (`BITRIX_WEBHOOK_URL`), а не через OAuth
«Локального приложения». Токены OAuth в общей базе обновляет главный сервис,
и если бы мы рефрешили их параллельно, два процесса регулярно затирали бы
друг другу refresh_token. У вебхука статичный URL и этой гонки нет.

На stdlib: ради трёх POST-запросов тянуть в прод ещё одну зависимость
незачем (по той же причине TOTP написан руками).
"""
import json
import logging
import ssl
import urllib.error
import urllib.request
from typing import Any, Optional

from .config import settings

log = logging.getLogger(__name__)

_TIMEOUT = 15  # сек на запрос; Битрикс отвечает быстро, а виснуть воркеру нельзя


class BitrixError(RuntimeError):
    """Битрикс ответил ошибкой или не ответил вовсе."""


def configured() -> bool:
    """Настроен ли вебхук. Если нет — работаем «вхолостую»: события пишем
    в лог, задачи не создаём. Так сервис поднимается и на машине разработчика."""
    return bool(settings.bitrix_webhook_url.strip())


def call(method: str, params: Optional[dict] = None) -> Any:
    """Вызов REST-метода. Возвращает содержимое поля `result`."""
    if not configured():
        raise BitrixError("BITRIX_WEBHOOK_URL не задан")

    base = settings.bitrix_webhook_url.strip().rstrip("/")
    url = f"{base}/{method}.json"
    body = json.dumps(params or {}).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT,
                                    context=ssl.create_default_context()) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # Тело ошибки Битрикса информативнее кода: там error_description.
        detail = e.read().decode("utf-8", "replace")[:400]
        raise BitrixError(f"{method}: HTTP {e.code} {detail}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise BitrixError(f"{method}: сеть недоступна ({e})") from e
    except ValueError as e:
        # Кривой адрес вебхука — urllib бросает ValueError. Наружу должен
        # уходить BitrixError: вызывающий код ловит его и продолжает, а не
        # падает целиком из-за одной опечатки в настройке.
        raise BitrixError(f"{method}: некорректный адрес вебхука ({e})") from e
    except json.JSONDecodeError as e:
        raise BitrixError(f"{method}: ответ не разобран как JSON") from e

    if isinstance(payload, dict) and payload.get("error"):
        raise BitrixError(
            f"{method}: {payload.get('error')} "
            f"{payload.get('error_description', '')}".strip())
    return payload.get("result") if isinstance(payload, dict) else payload


# ---------------------------------------------------------------------------
# Кто ведёт клиента
# ---------------------------------------------------------------------------
def _find_contact_id(email: str) -> int:
    """ID контакта по e-mail. `crm.duplicate.findbycomm` ищет по средству
    связи — в отличие от фильтра в crm.contact.list, который по вложенному
    полю EMAIL работает неочевидно."""
    res = call("crm.duplicate.findbycomm",
               {"entity_type": "CONTACT", "type": "EMAIL", "values": [email]})
    ids = (res or {}).get("CONTACT") or []
    return int(ids[0]) if ids else 0


def _deal_assignee(contact_id: int) -> int:
    """Ответственный по последней сделке контакта. Сделка точнее контакта:
    клиента могли завести давно, а ведёт его тот, кто взял сделку."""
    res = call("crm.deal.list", {
        "filter": {"CONTACT_ID": contact_id},
        "order": {"ID": "DESC"},
        "select": ["ID", "ASSIGNED_BY_ID"],
        "start": 0,
    })
    for deal in (res or []):
        if deal.get("ASSIGNED_BY_ID"):
            return int(deal["ASSIGNED_BY_ID"])
    return 0


def _contact_assignee(contact_id: int) -> int:
    res = call("crm.contact.get", {"id": contact_id})
    return int((res or {}).get("ASSIGNED_BY_ID") or 0)


def user_info(user_id: int) -> dict:
    """Карточка сотрудника: имя, должность, уволен или нет, тип учётной записи.

    Нужна, чтобы отсеять из таблицы менеджеров тех, кто менеджером не
    является: уволенных, роботов и интеграционные учётки.
    """
    if not user_id:
        return {}
    res = call("user.get", {"ID": user_id})
    rows = res if isinstance(res, list) else [res]
    if not rows or not rows[0]:
        return {}
    u = rows[0]
    full = " ".join(x for x in (u.get("NAME"), u.get("LAST_NAME")) if x).strip()
    active = u.get("ACTIVE")
    departments = u.get("UF_DEPARTMENT") or []
    if not isinstance(departments, list):
        departments = [departments]
    return {
        "name": full or u.get("EMAIL") or f"ID {user_id}",
        "position": (u.get("WORK_POSITION") or "").strip(),
        # Битрикс отдаёт то булево, то строку «Y»/«N» — приводим сами.
        "active": active if isinstance(active, bool) else str(active).upper() != "N",
        "user_type": (u.get("USER_TYPE") or "employee").lower(),
        "departments": ",".join(str(d) for d in departments if d),
    }


def user_name(user_id: int) -> str:
    """Имя сотрудника для отчётов. Пустая строка, если пользователь скрыт
    или удалён — падать из-за подписи не станем."""
    if not user_id:
        return ""
    try:
        res = call("user.get", {"ID": user_id})
    except BitrixError as e:
        log.warning("Не удалось получить имя сотрудника %s: %s", user_id, e)
        return ""
    rows = res if isinstance(res, list) else [res]
    if not rows:
        return ""
    u = rows[0] or {}
    full = " ".join(x for x in (u.get("NAME"), u.get("LAST_NAME")) if x).strip()
    return full or u.get("EMAIL") or f"ID {user_id}"


def resolve_manager(email: str) -> tuple[int, str]:
    """Кто ведёт клиента: (id, имя). (0, '') — если связать не удалось.

    Не найденный менеджер это не ошибка: клиент мог прийти не из CRM.
    Ошибки сети наверх пробрасываем — по ним воркер решает повторить позже.
    """
    contact_id = _find_contact_id(email)
    if not contact_id:
        return 0, ""
    manager_id = _deal_assignee(contact_id) or _contact_assignee(contact_id)
    if not manager_id:
        return 0, ""
    return manager_id, user_name(manager_id)


# ---------------------------------------------------------------------------
# Задачи
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Сделки и стадии
# ---------------------------------------------------------------------------
def _paged(method: str, params: dict, limit: int = 10000) -> list:
    """Обойти постраничный список. Битрикс отдаёт по 50 записей и возвращает
    смещение следующей страницы в `next`; без обхода мы увидели бы только
    первые 50 сделок и посчитали бы метрики по ним."""
    out: list = []
    start = 0
    while True:
        page = call(method, {**params, "start": start})
        # Часть методов возвращает список, часть — объект с `items`.
        items = page.get("items") if isinstance(page, dict) else page
        items = items or []
        out.extend(items)
        if len(out) >= limit or len(items) == 0:
            break
        start += len(items)
        # Битрикс перестаёт отдавать данные, когда страница неполная.
        if len(items) < 50:
            break
    return out[:limit]


def deal_stages(category_id: int) -> list[dict]:
    """Справочник стадий направления: id, название, порядок, семантика.

    Порядок берём из Битрикса (SORT), а не придумываем: воронка должна
    показываться в том же виде, в каком её видят в CRM.
    """
    res = call("crm.dealcategory.stage.list", {"id": int(category_id)})
    rows = []
    for s in (res or []):
        rows.append({
            "stage_id": s.get("STATUS_ID") or s.get("ID"),
            "name": s.get("NAME") or "",
            "sort": int(s.get("SORT") or 0),
            "semantics": s.get("SEMANTICS") or "",
        })
    return rows


def deals(category_id: int, modified_since: str = "",
          limit: int = 2000) -> list[dict]:
    """Сделки направления. `modified_since` — для инкрементального обновления."""
    flt: dict = {"CATEGORY_ID": int(category_id)}
    if modified_since:
        flt[">=DATE_MODIFY"] = modified_since
    return _paged("crm.deal.list", {
        "filter": flt,
        "order": {"DATE_MODIFY": "ASC"},
        "select": ["ID", "STAGE_ID", "CONTACT_ID", "ASSIGNED_BY_ID",
                   "DATE_CREATE", "DATE_MODIFY", "CLOSEDATE", "TITLE"],
    }, limit=limit)


def stage_history(category_id: int, since: str = "",
                  limit: int = 20000) -> list[dict]:
    """История переходов по стадиям (entityTypeId=2 — сделки).

    Из неё считается время на стадии: запись фиксирует момент входа в стадию,
    а длительность — это промежуток до следующей записи по той же сделке.
    """
    flt: dict = {"CATEGORY_ID": int(category_id)}
    if since:
        flt[">=CREATED_TIME"] = since
    return _paged("crm.stagehistory.list", {
        "entityTypeId": 2,
        "filter": flt,
        "order": {"CREATED_TIME": "ASC"},
        "select": ["ID", "OWNER_ID", "CREATED_TIME", "STAGE_ID",
                   "STAGE_SEMANTIC_ID"],
    }, limit=limit)


def create_task(title: str, description: str, responsible_id: int,
                deadline_iso: str = "") -> int:
    """Поставить задачу. Возвращает ID задачи (0 — если некому ставить)."""
    if not responsible_id:
        log.warning("Задача «%s» не создана: не задан ответственный", title)
        return 0
    fields = {
        "TITLE": title[:250],
        "DESCRIPTION": description,
        "RESPONSIBLE_ID": responsible_id,
    }
    if deadline_iso:
        fields["DEADLINE"] = deadline_iso
    res = call("tasks.task.add", {"fields": fields})
    task = (res or {}).get("task") or {}
    return int(task.get("id") or 0)
