"""Safe Telegram formatting for warehouse screens."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from html import escape
from typing import Iterable

from app.services.warehouse.quantity_service import SCALE

TELEGRAM_TEXT_LIMIT = 4096
_UNITS = {"kg": "кг", "l": "л", "pcs": "шт"}
_MONTHS = (
    "",
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)


def format_business_date(value: str) -> str:
    parsed = date.fromisoformat(value)
    return f"{parsed.day} {_MONTHS[parsed.month]} {parsed.year}"


def format_scaled(quantity_scaled: int, base_unit: str) -> str:
    value = Decimal(quantity_scaled) / SCALE
    rendered = format(value, "f").rstrip("0").rstrip(".").replace(".", ",")
    return f"{rendered} {_UNITS.get(base_unit, base_unit)}"


def split_telegram_lines(
    lines: Iterable[str],
    limit: int = TELEGRAM_TEXT_LIMIT,
) -> list[str]:
    chunks: list[str] = []
    current = ""
    for line in lines:
        safe_line = line
        while len(safe_line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(safe_line[:limit])
            safe_line = safe_line[limit:]
        candidate = safe_line if not current else f"{current}\n{safe_line}"
        if len(candidate) > limit:
            chunks.append(current)
            current = safe_line
        else:
            current = candidate
    if current or not chunks:
        chunks.append(current)
    return chunks


def batch_preview(batch: dict) -> list[str]:
    kind = str(batch["operation_type"])
    title = (
        f"🧪 Проработка: {escape(str(batch['development_title']))}"
        if kind == "development"
        else "♻️ Списание"
        if kind == "writeoff"
        else "🚚 Перемещение"
    )
    lines = ["<b>Проверьте записи:</b>", "", title, ""]
    for number, line in enumerate(batch["lines"], start=1):
        name = escape(str(line.get("product_name") or line.get("product_text") or "—"))
        quantity = format_scaled(int(line["quantity_scaled"]), str(line["base_unit"]))
        suffix = " <i>(единица подставлена)</i>" if not line.get("input_unit_text") else ""
        lines.append(f"{number}. {name} — {quantity}{suffix}")
        if line.get("reason_text"):
            lines.append(f"   Причина: {escape(str(line['reason_text']))}")
        if line.get("comment_text"):
            lines.append(f"   Комментарий: {escape(str(line['comment_text']))}")
        if batch["operation_type"] == "transfer":
            destination = escape(str(line.get("destination_name") or line.get("destination_text") or "—"))
            lines.append(f"   Бар → {destination}")
    lines.extend(("", f"Всего позиций: {len(batch['lines'])}"))
    return split_telegram_lines(lines)


def operation_text(operation: dict) -> str:
    transfer = operation["operation_type"] == "transfer"
    development = operation["operation_type"] == "development"
    lines = [
        (
            "🚚 <b>Перемещение</b>"
            if transfer
            else "🧪 <b>Проработка</b>"
            if development
            else "♻️ <b>Списание</b>"
        ),
        "",
        *(
            [f"Название: {escape(str(operation['development_title']))}"]
            if development
            else []
        ),
        f"Продукт: {escape(str(operation['product_name_snapshot']))}",
        f"Количество: {format_scaled(int(operation['quantity_scaled']), str(operation['base_unit']))}",
    ]
    if transfer:
        lines.extend(
            (
                f"Откуда: {escape(str(operation.get('source_location_name') or 'Бар'))}",
                f"Куда: {escape(str(operation.get('destination_location_name') or '—'))}",
            )
        )
    note = (
        operation.get("comment_text")
        if transfer or development
        else operation.get("reason_text")
    )
    if note:
        lines.append(
            f"{'Комментарий' if transfer or development else 'Причина'}: {escape(str(note))}"
        )
    lines.append(f"Статус: {'удалено' if operation.get('is_deleted') else 'активно'}")
    return "\n".join(lines)


def operations_list_text(
    operations: list[dict],
    *,
    operation_type: str,
    business_date: str,
    offset: int,
) -> str:
    title = {
        "writeoff": "♻️ Списания",
        "transfer": "🚚 Перемещения",
        "development": "🧪 Проработки",
    }[operation_type]
    lines = [f"<b>{title}</b>", format_business_date(business_date), ""]
    if not operations:
        lines.append("Записей пока нет.")
    for index, operation in enumerate(operations, start=offset + 1):
        lines.append(
            f"{index}. {escape(str(operation['product_name_snapshot']))} — "
            f"{format_scaled(int(operation['quantity_scaled']), str(operation['base_unit']))}"
        )
    return "\n".join(lines)


def aggregates_text(rows: list[dict], business_date: str) -> list[str]:
    lines = ["📊 <b>Итоги текущей смены</b>", format_business_date(business_date), ""]
    if not rows:
        lines.append("Нет активных записей.")
    for row in rows:
        icon = {"writeoff": "♻️", "transfer": "🚚", "development": "🧪"}[
            str(row["operation_type"])
        ]
        line = (
            f"{icon} {escape(str(row['product_name_snapshot']))} — "
            f"{format_scaled(int(row['quantity_scaled']), str(row['base_unit']))} "
            f"({int(row['operation_count'])} зап.)"
        )
        if row["operation_type"] == "transfer":
            source = escape(str(row.get("source_location_name") or "Бар"))
            destination = escape(
                str(row.get("destination_location_name") or "—")
            )
            line += f"\n   {source} → {destination}"
        elif row["operation_type"] == "development":
            line += f"\n   {escape(str(row['development_title']))}"
        lines.append(line)
    return split_telegram_lines(lines)
