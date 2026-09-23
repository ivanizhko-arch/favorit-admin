"""Служба заботы — обращения клиентов из приложения.

Пишет в таблицу care_messages основной backend (POST /care/message);
админка читает и группирует по клиентам, отдаёт тред и Excel-выгрузку.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from io import BytesIO
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from urllib.parse import quote

from . import db
from .security import get_admin

router = APIRouter(prefix="/admin/api/care", tags=["care"])

_MSK = timezone(timedelta(hours=3))


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _parse_ymd(s: str) -> Optional[datetime]:
    """'2026-09-01' → datetime в MSK (00:00)."""
    if not s:
        return None
    try:
        d = datetime.strptime(s, "%Y-%m-%d")
        return d.replace(tzinfo=_MSK)
    except ValueError:
        return None


@router.get("/clients")
def list_clients(_: str = Depends(get_admin)):
    """Список клиентов с обращениями — по одной карточке на email.
    Для каждого: имя, менеджер, всего обращений, из них новых, время последнего.
    Сортировка: сначала непрочитанные, потом по свежести."""
    with db._conn() as c:
        rows = c.execute(
            "SELECT email, "
            "  MAX(client_name) AS client_name, "
            "  MAX(client_phone) AS client_phone, "
            "  MAX(manager_name) AS manager_name, "
            "  MAX(deal_id) AS deal_id, "
            "  COUNT(*) AS total, "
            "  SUM(CASE WHEN seen = 0 THEN 1 ELSE 0 END) AS unseen, "
            "  MAX(created_at) AS last_at "
            "FROM care_messages "
            "GROUP BY email "
            "ORDER BY unseen DESC, last_at DESC"
        ).fetchall()
    return {"items": [dict(r) for r in rows]}


@router.get("/client/{email}")
def client_thread(email: str, _: str = Depends(get_admin)):
    """Все обращения одного клиента, старые сверху."""
    with db._conn() as c:
        rows = c.execute(
            "SELECT id, text, created_at, seen, deal_id, manager_name, "
            "  client_name, client_phone "
            "FROM care_messages WHERE email = ? "
            "ORDER BY created_at ASC",
            (email.lower(),),
        ).fetchall()
    if not rows:
        raise HTTPException(404, "Обращения не найдены")
    return {
        "email": email.lower(),
        "items": [dict(r) for r in rows],
    }


@router.post("/client/{email}/mark-seen")
def mark_seen(email: str, _: str = Depends(get_admin)):
    """Отметить все обращения клиента прочитанными для всей админки."""
    with db._conn() as c:
        cur = c.execute(
            "UPDATE care_messages SET seen = 1 "
            "WHERE email = ? AND seen = 0",
            (email.lower(),),
        )
    return {"ok": True, "updated": int(getattr(cur, "rowcount", 0) or 0)}


@router.get("/unseen-count")
def unseen_count(_: str = Depends(get_admin)):
    """Общее число непрочитанных обращений — для бейджа на вкладке."""
    with db._conn() as c:
        r = c.execute(
            "SELECT COUNT(*) n FROM care_messages WHERE seen = 0"
        ).fetchone()
    return {"unseen": int(r["n"] if r else 0)}


@router.get("/export.xlsx")
def export_xlsx(
    date_from: str = "",
    date_to: str = "",
    _: str = Depends(get_admin),
):
    """Excel-выгрузка: Дата | Клиент | Менеджер | № сделки | Текст.
    Фильтр по датам (MSK). Пустые даты → всё за всё время."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    df = _parse_ymd(date_from)
    dt_to = _parse_ymd(date_to)
    if df and dt_to and dt_to < df:
        df, dt_to = dt_to, df

    sql = ("SELECT created_at, email, client_name, client_phone, "
           "manager_name, deal_id, text "
           "FROM care_messages")
    args: list = []
    where: list = []
    if df:
        where.append("created_at >= ?")
        args.append(df.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"))
    if dt_to:
        where.append("created_at < ?")
        args.append((dt_to + timedelta(days=1)).astimezone(timezone.utc)
                    .strftime("%Y-%m-%dT%H:%M:%S"))
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC"

    with db._conn() as c:
        rows = c.execute(sql, args).fetchall()

    wb = Workbook()
    ws = wb.active
    ws.title = "Служба заботы"

    gold = PatternFill(start_color="D7AE5E", end_color="D7AE5E",
                       fill_type="solid")
    bold_white = Font(bold=True, color="FFFFFF")
    wrap = Alignment(wrap_text=True, vertical="top")

    headers = ["Дата (МСК)", "Клиент", "Email", "Телефон",
               "Менеджер", "№ сделки", "Текст обращения"]
    for i, h in enumerate(headers, start=1):
        cell = ws.cell(row=1, column=i, value=h)
        cell.font = bold_white
        cell.fill = gold
        cell.alignment = Alignment(horizontal="center", vertical="center")

    for idx, r in enumerate(rows, start=2):
        rd = dict(r)
        try:
            dt_utc = datetime.fromisoformat(rd["created_at"].replace("Z", ""))
            dt_utc = dt_utc.replace(tzinfo=timezone.utc)
            dt_msk = dt_utc.astimezone(_MSK).strftime("%d.%m.%Y %H:%M")
        except Exception:  # noqa: BLE001
            dt_msk = rd.get("created_at") or ""

        ws.cell(row=idx, column=1, value=dt_msk)
        ws.cell(row=idx, column=2, value=rd.get("client_name") or "")
        ws.cell(row=idx, column=3, value=rd.get("email") or "")
        ws.cell(row=idx, column=4, value=rd.get("client_phone") or "")
        ws.cell(row=idx, column=5, value=rd.get("manager_name") or "")
        ws.cell(row=idx, column=6,
                value=int(rd["deal_id"]) if rd.get("deal_id") else "")
        text_cell = ws.cell(row=idx, column=7, value=rd.get("text") or "")
        text_cell.alignment = wrap

    widths = [18, 26, 30, 18, 24, 12, 80]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[chr(64 + i)].width = w

    ws.freeze_panes = "A2"

    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)

    parts = []
    if df:
        parts.append(df.strftime("%Y-%m-%d"))
    if dt_to:
        parts.append(dt_to.strftime("%Y-%m-%d"))
    suffix = "_".join(parts) if parts else "all"
    filename = f"Служба заботы_{suffix}.xlsx"

    return StreamingResponse(
        buf,
        media_type=("application/vnd.openxmlformats-officedocument."
                    "spreadsheetml.sheet"),
        headers={
            "Content-Disposition": (
                f"attachment; filename=\"care.xlsx\"; "
                f"filename*=UTF-8''{quote(filename)}"
            ),
        },
    )
