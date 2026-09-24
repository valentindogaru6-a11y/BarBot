"""On-demand, read-only XLSX timesheets for personal shift sessions."""

from __future__ import annotations

import calendar
import threading
import time
from collections import defaultdict
from datetime import UTC, date, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any
from zoneinfo import ZoneInfo

import xlsxwriter

from app.services.access_service import AccessService
from app.services.warehouse.xlsx_report_service import _validate_xlsx, safe_excel_text
from app.storage.warehouse_attendance_database import WarehouseAttendanceDatabase


class AttendanceReportCoordinator:
    """Single-process claim for one requester/period/token report request."""

    def __init__(self, completed_ttl_seconds: float = 120.0) -> None:
        self.completed_ttl_seconds = completed_ttl_seconds
        self._lock = threading.Lock()
        self._claims: dict[tuple[int, str], tuple[int, str, float]] = {}

    def claim(self, user_id: int, period: str, token: int) -> bool:
        key = (user_id, period)
        now = time.monotonic()
        with self._lock:
            current = self._claims.get(key)
            if current is not None:
                old_token, status, changed = current
                if status == "running" or (old_token == token and now - changed < self.completed_ttl_seconds):
                    return False
            self._claims[key] = (token, "running", now)
            return True

    def finish(self, user_id: int, period: str, token: int) -> None:
        key = (user_id, period)
        with self._lock:
            if self._claims.get(key, (None,))[0] == token:
                self._claims[key] = (token, "completed", time.monotonic())

    def release(self, user_id: int, period: str, token: int) -> None:
        key = (user_id, period)
        with self._lock:
            if self._claims.get(key, (None,))[0] == token:
                self._claims.pop(key, None)


def parse_month(value: str) -> tuple[date, date]:
    if len(value) != 7 or value[4] != "-":
        raise ValueError("Введите месяц в формате YYYY-MM")
    try:
        year, month = map(int, value.split("-"))
        first = date(year, month, 1)
    except ValueError as exc:
        raise ValueError("Введите корректный месяц в формате YYYY-MM") from exc
    if not 2000 <= year <= 2100:
        raise ValueError("Год должен быть от 2000 до 2100")
    return first, date(year, month, calendar.monthrange(year, month)[1])


def _dt(value: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("Attendance timestamp must be UTC RFC3339 with Z")
    parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise ValueError("Attendance timestamp must be UTC")
    return parsed


def _duration(row: dict[str, Any]) -> int | None:
    if not row.get("closed_at_utc"):
        return None
    seconds = int((_dt(row["closed_at_utc"]) - _dt(row["opened_at_utc"])).total_seconds())
    if seconds < 0:
        raise ValueError("Attendance close precedes open")
    return seconds


def _hm(seconds: int | None) -> str:
    return "" if seconds is None else f"{seconds // 3600} ч {(seconds % 3600) // 60:02d} мин"


class WarehouseAttendanceXlsxService:
    def __init__(self, repository: WarehouseAttendanceDatabase, access: AccessService,
                 temporary_directory: str | Path, timezone: str = "Europe/Kyiv") -> None:
        self.repository = repository
        self.access = access
        self.directory = Path(temporary_directory)
        self.zone = ZoneInfo(timezone)

    def build_month(self, actor_user_id: int, month: str) -> Path:
        self.access.require_warehouse_attendance_management(actor_user_id)
        first, last = parse_month(month)
        return self._build(actor_user_id, first, last, month)

    def build_day(self, actor_user_id: int, business_date: date) -> Path:
        self.access.require_warehouse_attendance_management(actor_user_id)
        return self._build(actor_user_id, business_date, business_date, business_date.isoformat())

    def _build(self, actor_user_id: int, first: date, last: date, label: str) -> Path:
        self.access.require_warehouse_attendance_management(actor_user_id)
        rows = self.repository.sessions_between(first.isoformat(), last.isoformat())
        self.directory.mkdir(parents=True, exist_ok=True)
        handle = NamedTemporaryFile(prefix=f"warehouse_shift_attendance_{label}_",
                                   suffix=".xlsx", dir=self.directory, delete=False)
        path = Path(handle.name); handle.close()
        try:
            self._write(path, rows, first, last)
            _validate_xlsx(path)
            return path
        except Exception:
            path.unlink(missing_ok=True)
            raise

    def _write(self, path: Path, rows: list[dict[str, Any]], first: date, last: date) -> None:
        workbook = xlsxwriter.Workbook(path, {"strings_to_formulas": False, "strings_to_urls": False})
        header = workbook.add_format({"bold": True, "bg_color": "#D9EAD3", "border": 1})
        open_fmt = workbook.add_format({"bg_color": "#FFF2CC"})
        force_fmt = workbook.add_format({"bg_color": "#F4CCCC"})
        sheets = workbook.add_worksheet("Смены"), workbook.add_worksheet("Итоги по сотрудникам"), workbook.add_worksheet("Календарь")
        shifts, totals, cal = sheets
        columns = ["Дата смены","Сотрудник","Username","Роль","Локация","Открытие","Закрытие","Продолжительность","Статус","Кем открыта","Кем закрыта","Причина принудительного закрытия"]
        shifts.write_row(0, 0, columns, header); shifts.freeze_panes(1, 0); shifts.autofilter(0, 0, max(1,len(rows)), len(columns)-1)
        shifts.set_column(0, 0, 13); shifts.set_column(1, 4, 22); shifts.set_column(5, 11, 24)
        by_user: dict[tuple[str,str], list[dict[str,Any]]] = defaultdict(list)
        by_day_user: dict[tuple[str,str], list[dict[str,Any]]] = defaultdict(list)
        status_labels = {"open":"Открыта","closed":"Закрыта","force_closed":"Закрыта принудительно"}
        for index, row in enumerate(rows, 1):
            start = _dt(row["opened_at_utc"]).astimezone(self.zone)
            end = _dt(row["closed_at_utc"]).astimezone(self.zone) if row.get("closed_at_utc") else None
            duration = _duration(row)
            opener = " ".join(filter(None,(row.get("opener_first_name"),row.get("opener_last_name"))))
            closer = " ".join(filter(None,(row.get("closer_first_name"),row.get("closer_last_name"))))
            values = [row["business_date"],row["display_name_snapshot"],row.get("username_snapshot") or "",row.get("user_role") or "",row["source_location_name"],start.strftime("%d.%m.%Y %H:%M"),end.strftime("%d.%m.%Y %H:%M") if end else "",_hm(duration),status_labels[row["status"]],opener,closer,row.get("close_reason") or ""]
            fmt = open_fmt if row["status"] == "open" else force_fmt if row["status"] == "force_closed" else None
            shifts.write_row(index, 0, [safe_excel_text(v) for v in values], fmt)
            key=(str(row["user_id"]),row["display_name_snapshot"]); by_user[key].append(row); by_day_user[(key[0],row["business_date"])].append(row)
        total_headers=["Сотрудник","Количество завершённых смен","Общее подтверждённое время","Средняя длительность","Незакрытые смены","Принудительно закрытые смены","Первая смена периода","Последняя смена периода"]
        totals.write_row(0,0,total_headers,header); totals.freeze_panes(1,0); totals.set_column(0,7,24)
        for i, ((_,name), items) in enumerate(sorted(by_user.items(), key=lambda item:item[0][1]),1):
            closed=[x for x in items if x["status"] in {"closed","force_closed"}]
            seconds=sum(_duration(x) or 0 for x in closed)
            values=[name,len([x for x in items if x["status"]=="closed"]),_hm(seconds),_hm(seconds//len(closed)) if closed else "",len([x for x in items if x["status"]=="open"]),len([x for x in items if x["status"]=="force_closed"]),min(x["business_date"] for x in items),max(x["business_date"] for x in items)]
            totals.write_row(i,0,[safe_excel_text(v) for v in values])
        days=[]; current=first
        from datetime import timedelta
        while current<=last: days.append(current); current+=timedelta(days=1)
        cal.write_row(0,0,["Сотрудник",*[str(d.day) for d in days],"Итого"],header); cal.freeze_panes(1,1); cal.set_column(0,0,24); cal.set_column(1,len(days),12)
        for r, ((uid,name), items) in enumerate(sorted(by_user.items(), key=lambda item:item[0][1]),1):
            cal.write(r,0,safe_excel_text(name)); total_seconds=0
            for c, day in enumerate(days,1):
                sessions=by_day_user.get((uid,day.isoformat()),[])
                seconds=sum(_duration(x) or 0 for x in sessions if x["status"] in {"closed","force_closed"})
                total_seconds+=seconds
                has_open=any(x["status"]=="open" for x in sessions)
                value=(f"{_hm(seconds)} + ОТКРЫТА" if seconds and has_open else "ОТКРЫТА" if has_open else _hm(seconds) if sessions else "")
                cal.write(r,c,value)
            cal.write(r,len(days)+1,_hm(total_seconds))
        workbook.close()
