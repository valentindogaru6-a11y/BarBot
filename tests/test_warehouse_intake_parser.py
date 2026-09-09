import pytest

from app.models.warehouse.operations import WarehouseOperationType
from app.services.warehouse.intake_parser import IntakeParseError, parse_intake_message


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
        ("списание: сахар xx", "invalid_line"),
    ],
)
def test_invalid_grammar(text, code) -> None:
    with pytest.raises(IntakeParseError) as caught:
        parse_intake_message(text)
    assert caught.value.code == code


def test_reason_and_raw_length_limits() -> None:
    with pytest.raises(IntakeParseError, match="длин"):
        parse_intake_message("списание: сахар 1 (" + "x" * 501 + ")")
    with pytest.raises(IntakeParseError, match="длин"):
        parse_intake_message("списание: " + "x" * 2001)
