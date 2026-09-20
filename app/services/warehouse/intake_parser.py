"""Strict anchored grammar for warehouse intake messages."""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.models.warehouse.operations import WarehouseOperationType

MAX_RAW_TEXT_LENGTH = 20_000
MAX_LINE_LENGTH = 2_000
MAX_NAME_LENGTH = 200
MAX_NOTE_LENGTH = 500

_BASIC_TRIGGER_RE = re.compile(
    r"^(?P<kind>списание|перемещение|проработка):(?P<tail>.*)$",
    re.IGNORECASE,
)
_TRANSFER_HEADER_RE = re.compile(
    r"^перемещение\s+(?:на|в)\s+(?P<destination>.+?):\s*$",
    re.IGNORECASE,
)
_UNIT_PATTERN = (
    r"кг|kg|килограмм(?:а|ов)?|г|гр|g|грамм(?:а|ов)?|"
    r"л|l|литр(?:а|ов)?|мл|ml|миллилитр(?:а|ов)?|"
    r"шт\.|шт|pcs|штук(?:а|и)?"
)
_NUMBER_PATTERN = r"(?:\d+(?:[.,]\d+)?|[.,]\d+)"
_LEFT_RE = re.compile(
    rf"^(?P<product>.+?)\s+(?P<quantity>{_NUMBER_PATTERN})"
    r"(?:\s*(?P<unit>\S+))?$",
    re.IGNORECASE,
)
_INVALID_QUANTITY_WITH_UNIT_RE = re.compile(
    rf"^(?P<product>.+?)\s+(?P<quantity>\S+)\s+(?P<unit>{_UNIT_PATTERN})$",
    re.IGNORECASE,
)
_INVALID_QUANTITY_RE = re.compile(
    r"^(?P<product>.+?)\s+(?P<quantity>\S+)$",
    re.IGNORECASE,
)


class IntakeParseError(ValueError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        line_number: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.line_number = line_number


@dataclass(frozen=True, slots=True)
class ParsedIntakeHeader:
    operation_type: WarehouseOperationType
    development_title: str | None = None
    header_destination_text: str | None = None
    inline_body: str | None = None


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
    development_title: str | None = None
    header_destination_text: str | None = None


def parse_intake_header(value: str) -> ParsedIntakeHeader:
    """Parse only a complete first-line trigger without guessing intent."""

    text = value.strip()
    transfer = _TRANSFER_HEADER_RE.fullmatch(text)
    if transfer is not None:
        destination = transfer.group("destination").strip()
        if not destination or len(destination) > MAX_NAME_LENGTH:
            raise IntakeParseError(
                "invalid_destination",
                "Некорректное место назначения",
            )
        return ParsedIntakeHeader(
            WarehouseOperationType.TRANSFER,
            header_destination_text=destination,
        )

    trigger = _BASIC_TRIGGER_RE.fullmatch(text)
    if trigger is None:
        raise IntakeParseError(
            "not_trigger",
            "Первая строка не является точным trigger",
        )
    kind = trigger.group("kind").casefold()
    tail = trigger.group("tail").strip()
    if kind == "списание":
        return ParsedIntakeHeader(
            WarehouseOperationType.WRITEOFF,
            inline_body=tail or None,
        )
    if kind == "перемещение":
        return ParsedIntakeHeader(
            WarehouseOperationType.TRANSFER,
            inline_body=tail or None,
        )
    if not tail:
        raise IntakeParseError(
            "missing_development_title",
            "Укажите название проработки после двоеточия",
        )
    if len(tail) > MAX_NAME_LENGTH:
        raise IntakeParseError(
            "development_title_too_long",
            "Название проработки слишком длинное",
        )
    return ParsedIntakeHeader(
        WarehouseOperationType.DEVELOPMENT,
        development_title=tail,
    )


def is_warehouse_trigger_header(value: str) -> bool:
    try:
        parse_intake_header(value)
    except IntakeParseError as exc:
        return exc.code == "missing_development_title"
    return True


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
        raise IntakeParseError(
            "empty_note",
            "Пустой комментарий",
            line_number=line_number,
        )
    if len(note) > MAX_NOTE_LENGTH:
        raise IntakeParseError(
            "note_too_long",
            "Комментарий слишком длинный",
            line_number=line_number,
        )
    return value[: match.start()].rstrip(), note


def _parse_left(value: str, line_number: int) -> tuple[str, str, str | None]:
    match = _LEFT_RE.fullmatch(value.strip())
    if match is None:
        # Preserve the product part even when the quantity token is malformed.
        # This lets the service report an invalid quantity independently from
        # product matching instead of treating the complete line as a name.
        match = _INVALID_QUANTITY_WITH_UNIT_RE.fullmatch(value.strip())
        if match is None:
            match = _INVALID_QUANTITY_RE.fullmatch(value.strip())
        if match is None:
            raise IntakeParseError(
                "invalid_line",
                "Ожидаются название продукта и количество",
                line_number=line_number,
            )
    product = match.group("product").strip()
    if not product or len(product) > MAX_NAME_LENGTH:
        raise IntakeParseError(
            "invalid_product",
            "Некорректное название продукта",
            line_number=line_number,
        )
    return product, match.group("quantity"), match.groupdict().get("unit")


def parse_intake_message(raw_text: str) -> ParsedIntakeMessage:
    if not isinstance(raw_text, str) or not raw_text.strip():
        raise IntakeParseError(
            "not_trigger",
            "Сообщение не содержит складской команды",
        )
    if len(raw_text) > MAX_RAW_TEXT_LENGTH:
        raise IntakeParseError("message_too_long", "Сообщение слишком длинное")
    source_lines = raw_text.splitlines()
    first_index = next(
        (index for index, line in enumerate(source_lines) if line.strip()),
        None,
    )
    if first_index is None:
        raise IntakeParseError(
            "not_trigger",
            "Сообщение не содержит складской команды",
        )
    header = parse_intake_header(source_lines[first_index])
    body: list[str] = []
    if header.inline_body:
        body.append(header.inline_body)
    body.extend(
        line.strip()
        for line in source_lines[first_index + 1 :]
        if line.strip()
    )
    if not body:
        raise IntakeParseError("empty_body", "После trigger нет позиций")

    parsed: list[ParsedIntakeLine] = []
    for line_number, raw_line in enumerate(body, start=1):
        if len(raw_line) > MAX_LINE_LENGTH:
            raise IntakeParseError(
                "line_too_long",
                "Строка слишком длинная",
                line_number=line_number,
            )
        core, note = _extract_final_note(raw_line, line_number)
        destination: str | None = None
        if header.operation_type is WarehouseOperationType.TRANSFER:
            if header.header_destination_text is not None:
                if "->" in core:
                    raise IntakeParseError(
                        "header_destination_conflict",
                        "Не используйте стрелку, если место указано в заголовке",
                        line_number=line_number,
                    )
                left = core
                destination = header.header_destination_text
            else:
                if core.count("->") != 1:
                    raise IntakeParseError(
                        "invalid_transfer_arrow",
                        "Перемещение требует ровно одну стрелку ->",
                        line_number=line_number,
                    )
                left, destination = (part.strip() for part in core.split("->"))
                if not destination or len(destination) > MAX_NAME_LENGTH:
                    raise IntakeParseError(
                        "invalid_destination",
                        "Некорректное место назначения",
                        line_number=line_number,
                    )
        else:
            if "->" in core:
                raise IntakeParseError(
                    "unexpected_arrow",
                    "Стрелка допустима только для перемещения",
                    line_number=line_number,
                )
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
                reason_text=(
                    note
                    if header.operation_type is WarehouseOperationType.WRITEOFF
                    else None
                ),
                comment_text=(
                    note
                    if header.operation_type
                    in {
                        WarehouseOperationType.TRANSFER,
                        WarehouseOperationType.DEVELOPMENT,
                    }
                    else None
                ),
            )
        )
    return ParsedIntakeMessage(
        header.operation_type,
        raw_text,
        tuple(parsed),
        development_title=header.development_title,
        header_destination_text=header.header_destination_text,
    )
