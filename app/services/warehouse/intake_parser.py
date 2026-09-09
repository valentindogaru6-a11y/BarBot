"""Strict free-text grammar for warehouse intake messages."""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.models.warehouse.operations import WarehouseOperationType

MAX_RAW_TEXT_LENGTH = 20_000
MAX_LINE_LENGTH = 2_000
MAX_NAME_LENGTH = 200
MAX_NOTE_LENGTH = 500

_TRIGGER_RE = re.compile(r"^(списание|перемещение):(?:\s*(.*))?$", re.IGNORECASE)
_UNIT_PATTERN = (
    r"кг|kg|килограмм(?:а|ов)?|г|гр|g|грамм(?:а|ов)?|"
    r"л|l|литр(?:а|ов)?|мл|ml|миллилитр(?:а|ов)?|"
    r"шт\.|шт|pcs|штук(?:а|и)?"
)
_LEFT_RE = re.compile(
    rf"^(?P<product>.+?)\s+(?P<quantity>(?:\d+(?:[.,]\d+)?|[.,]\d+))"
    rf"\s*(?P<unit>{_UNIT_PATTERN})?$",
    re.IGNORECASE,
)


class IntakeParseError(ValueError):
    def __init__(self, code: str, message: str, *, line_number: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.line_number = line_number


@dataclass(frozen=True, slots=True)
class ParsedIntakeLine:
    line_number: int
    raw_line: str
    product_text: str
    quantity_text: str
    unit_text: str | None
    destination_text: str | None
    reason_text: str | None
    comment_text: str | None


@dataclass(frozen=True, slots=True)
class ParsedIntakeMessage:
    operation_type: WarehouseOperationType
    raw_text: str
    lines: tuple[ParsedIntakeLine, ...]


def _extract_final_note(value: str, line_number: int) -> tuple[str, str | None]:
    if "(" not in value and ")" not in value:
        return value.strip(), None
    match = re.search(r"\(([^()]*)\)\s*$", value)
    if match is None or "(" in value[: match.start()] or ")" in value[: match.start()]:
        raise IntakeParseError(
            "malformed_parentheses",
            "Комментарий должен быть в одной паре скобок в конце строки",
            line_number=line_number,
        )
    note = match.group(1).strip()
    if not note:
        raise IntakeParseError("empty_note", "Пустой комментарий", line_number=line_number)
    if len(note) > MAX_NOTE_LENGTH:
        raise IntakeParseError("note_too_long", "Комментарий слишком длинный", line_number=line_number)
    return value[: match.start()].rstrip(), note


def _parse_left(value: str, line_number: int) -> tuple[str, str, str | None]:
    match = _LEFT_RE.fullmatch(value.strip())
    if match is None:
        raise IntakeParseError(
            "invalid_line",
            "Ожидаются название продукта и положительное количество",
            line_number=line_number,
        )
    product = match.group("product").strip()
    if not product or len(product) > MAX_NAME_LENGTH:
        raise IntakeParseError("invalid_product", "Некорректное название продукта", line_number=line_number)
    return product, match.group("quantity"), match.group("unit")


def parse_intake_message(raw_text: str) -> ParsedIntakeMessage:
    if not isinstance(raw_text, str) or not raw_text.strip():
        raise IntakeParseError("not_trigger", "Сообщение не содержит складской команды")
    if len(raw_text) > MAX_RAW_TEXT_LENGTH:
        raise IntakeParseError("message_too_long", "Сообщение слишком длинное")
    lines = raw_text.splitlines()
    first_index = next((index for index, line in enumerate(lines) if line.strip()), None)
    if first_index is None:
        raise IntakeParseError("not_trigger", "Сообщение не содержит складской команды")
    trigger = _TRIGGER_RE.fullmatch(lines[first_index].strip())
    if trigger is None:
        raise IntakeParseError("not_trigger", "Первая строка не является точным trigger")
    operation_type = (
        WarehouseOperationType.WRITEOFF
        if trigger.group(1).casefold() == "списание"
        else WarehouseOperationType.TRANSFER
    )
    body: list[str] = []
    inline = (trigger.group(2) or "").strip()
    if inline:
        body.append(inline)
    body.extend(line.strip() for line in lines[first_index + 1 :] if line.strip())
    if not body:
        raise IntakeParseError("empty_body", "После trigger нет позиций")

    parsed: list[ParsedIntakeLine] = []
    for line_number, raw_line in enumerate(body, start=1):
        if len(raw_line) > MAX_LINE_LENGTH:
            raise IntakeParseError("line_too_long", "Строка слишком длинная", line_number=line_number)
        core, note = _extract_final_note(raw_line, line_number)
        destination: str | None = None
        if operation_type is WarehouseOperationType.TRANSFER:
            if core.count("->") != 1:
                raise IntakeParseError(
                    "invalid_transfer_arrow",
                    "Перемещение требует ровно одну стрелку ->",
                    line_number=line_number,
                )
            left, destination = (part.strip() for part in core.split("->"))
            if not destination or len(destination) > MAX_NAME_LENGTH:
                raise IntakeParseError(
                    "invalid_destination", "Некорректное место назначения", line_number=line_number
                )
        else:
            if "->" in core:
                raise IntakeParseError("unexpected_arrow", "Стрелка допустима только для перемещения", line_number=line_number)
            left = core
        product, quantity, unit = _parse_left(left, line_number)
        parsed.append(
            ParsedIntakeLine(
                line_number=line_number,
                raw_line=raw_line,
                product_text=product,
                quantity_text=quantity,
                unit_text=unit,
                destination_text=destination,
                reason_text=note if operation_type is WarehouseOperationType.WRITEOFF else None,
                comment_text=note if operation_type is WarehouseOperationType.TRANSFER else None,
            )
        )
    return ParsedIntakeMessage(operation_type, raw_text, tuple(parsed))
