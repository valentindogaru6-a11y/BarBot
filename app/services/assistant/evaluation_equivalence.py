"""Scorer-only equivalence; never influences provider input or execution."""

from decimal import Decimal, InvalidOperation

_UNITS = {}
for aliases, dimension, factor in (
    (('g', 'gram', 'grams', 'г', 'гр', 'грамм', 'грамма', 'граммов'), 'mass', '1'),
    (('kg', 'кг', 'килограмм'), 'mass', '1000'),
    (('ml', 'мл'), 'volume', '1'),
    (('l', 'л'), 'volume', '1000'),
    (('pcs', 'шт'), 'count', '1'),
):
    for alias in aliases:
        _UNITS[alias] = dimension, Decimal(factor)


def quantity_equivalent(quantity, unit, expected_quantity, expected_unit):
    try:
        if isinstance(quantity, (float, bool)) or isinstance(expected_quantity, (float, bool)):
            return False
        actual = Decimal(quantity)
        expected = Decimal(expected_quantity)
        if (not actual.is_finite() or not expected.is_finite()
            or abs(actual.adjusted()) > 100 or abs(expected.adjusted()) > 100):
            return False
        actual_dimension, actual_factor = _UNITS[unit.strip().casefold()]
        expected_dimension, expected_factor = _UNITS[expected_unit.strip().casefold()]
        return (actual.is_finite() and expected.is_finite() and actual >= 0 and expected >= 0
            and actual_dimension == expected_dimension
            and actual * actual_factor == expected * expected_factor)
    except (InvalidOperation, ValueError, TypeError, KeyError, AttributeError):
        return False


def informational_read_only(actual):
    from app.services.assistant.capabilities import CAPABILITIES
    registered = {c.id: c for c in CAPABILITIES}
    capability = actual.get('capability')
    definition = registered.get(capability)
    return (actual.get('domain') in {'general', 'warehouse'}
        and actual.get('mutation') is False and actual.get('read_only') is True
        and (capability is None or (definition is not None and definition.risk_level.value == 0))
        and actual.get('preview') is False and actual.get('safety') is True
        and actual.get('operation_delta', actual.get('operations', 0)) == 0
        and all(actual.get(key, 0) == 0 for key in (
            'draft_delta', 'proposal_delta',
            'unexpected_state_change_count', 'false_mutation_count',
        )))
