import pytest

from app.models.warehouse.operations import WarehouseOperationType
from app.services.warehouse.intake_parser import (
    IntakeParseError,
    is_warehouse_trigger_header,
    parse_intake_message,
)


def test_one_line_and_multiline_writeoff() -> None:
    single = parse_intake_message("СпИсАнИе: апельсин 0.15")
    assert single.operation_type is WarehouseOperationType.WRITEOFF
    assert single.lines[0].product_text == "апельсин"
    multiple = parse_intake_message(
        "\nсписание:\n\nапельсин 0.15\nсок яблочный 0,2 л\nкомпот 2л\n"
    )
    assert len(multiple.lines) == 3
    assert multiple.lines[-1].unit_text == "л"


def test_reason_transfer_comment_and_product_digits() -> None:
    written = parse_intake_message("списание: сироп 1883 1 л (просрочен)")
    assert written.lines[0].product_text == "сироп 1883"
    assert written.lines[0].reason_text == "просрочен"
    moved = parse_intake_message(
        "перемещение:\nкокосовое молоко 0.25 л -> кондитерка (для десертов)"
    )
    assert moved.lines[0].destination_text == "кондитерка"
    assert moved.lines[0].comment_text == "для десертов"


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("нужно сделать списание: апельсин 1", "not_trigger"),
        ("описание со словом списание", "not_trigger"),
        ("/списание: апельсин 1", "not_trigger"),
        ("списание:", "empty_body"),
        ("списание: апельсин 1 (плохо", "malformed_parentheses"),
        ("списание: апельсин 1 (а) (б)", "malformed_parentheses"),
        ("перемещение: сахар 1 кг кухня", "invalid_transfer_arrow"),
        ("перемещение: сахар 1 кг -> кухня -> бар", "invalid_transfer_arrow"),
        ("перемещение: сахар 1 кг -> ", "invalid_destination"),
    ],
)
def test_invalid_grammar(text, code) -> None:
    with pytest.raises(IntakeParseError) as caught:
        parse_intake_message(text)
    assert caught.value.code == code


def test_missing_or_unknown_unit_does_not_pollute_product_text() -> None:
    missing = parse_intake_message(
        "перемещение:\nкокосовое молоко 0.25 -> кухня"
    ).lines[0]
    assert missing.product_text == "кокосовое молоко"
    assert missing.quantity_text == "0.25"
    assert missing.unit_text is None
    assert missing.destination_text == "кухня"

    unknown = parse_intake_message("списание: сахар 1 ведро").lines[0]
    assert unknown.product_text == "сахар"
    assert unknown.quantity_text == "1"
    assert unknown.unit_text == "ведро"

    invalid = parse_intake_message("списание: сахар много").lines[0]
    assert invalid.product_text == "сахар"
    assert invalid.quantity_text == "много"

    multiword = parse_intake_message(
        "списание: кокосовое молоко много"
    ).lines[0]
    assert multiword.product_text == "кокосовое молоко"
    assert multiword.quantity_text == "много"


def test_reason_and_raw_length_limits() -> None:
    with pytest.raises(IntakeParseError, match="длин"):
        parse_intake_message("списание: сахар 1 (" + "x" * 501 + ")")
    with pytest.raises(IntakeParseError, match="длин"):
        parse_intake_message("списание: " + "x" * 2001)


def test_development_trigger_title_body_and_comment() -> None:
    parsed = parse_intake_message(
        "ПрОрАбОтКа: Новый чай\n"
        "облепиха 0.25 кг\n"
        "сахарный сироп 0.15 л (баланс сладости)"
    )
    assert parsed.operation_type is WarehouseOperationType.DEVELOPMENT
    assert parsed.development_title == "Новый чай"
    assert len(parsed.lines) == 2
    assert parsed.lines[1].comment_text == "баланс сладости"
    assert parsed.lines[1].reason_text is None


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("проработка:\nапельсин 0.2 кг", "missing_development_title"),
        ("проработка: Чай", "empty_body"),
        ("нужно сделать проработку: Чай", "not_trigger"),
        ("сегодня была проработка нового чая", "not_trigger"),
        ("/проработка", "not_trigger"),
    ],
)
def test_development_trigger_is_strict(text: str, code: str) -> None:
    with pytest.raises(IntakeParseError) as caught:
        parse_intake_message(text)
    assert caught.value.code == code


def test_transfer_header_destination_and_arrow_conflict() -> None:
    moved = parse_intake_message(
        "перемещение на кухню:\nмолоко 1 л\nсахар 0.5 кг"
    )
    assert moved.operation_type is WarehouseOperationType.TRANSFER
    assert moved.header_destination_text == "кухню"
    assert {line.destination_text for line in moved.lines} == {"кухню"}
    into = parse_intake_message("ПЕРЕМЕЩЕНИЕ В кондитерку:\nмолоко 1 л")
    assert into.lines[0].destination_text == "кондитерку"
    with pytest.raises(IntakeParseError) as caught:
        parse_intake_message("перемещение на кухню:\nмолоко 1 л -> кухня")
    assert caught.value.code == "header_destination_conflict"
    assert parse_intake_message(
        "перемещение:\nмолоко 1 л -> кухня"
    ).lines[0].destination_text == "кухня"


def test_trigger_filter_helper_does_not_guess() -> None:
    assert is_warehouse_trigger_header("проработка: Чай")
    assert is_warehouse_trigger_header("проработка:")
    assert is_warehouse_trigger_header("перемещение в кухню:")
    assert not is_warehouse_trigger_header("сегодня была проработка")
