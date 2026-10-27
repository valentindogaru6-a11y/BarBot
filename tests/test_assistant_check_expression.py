"""Deterministic correctness and linear-work regressions for CHECK normalization."""

from collections.abc import Sequence

import pytest

from app.storage.assistant_memory_schema import (
    AssistantMemorySchemaError,
    _checks,
    _expression,
    _normalized,
    _tokens,
)


@pytest.mark.parametrize(('expression', 'expected'), (
    ('revision>0', 'revision>0'),
    ('(revision>0)', 'revision>0'),
    ('((((revision>0))))', 'revision>0'),
    ('((revision>0 AND status IS NOT NULL))', 'revision>0 AND status IS NOT NULL'),
    ('(revision)>0', '(revision)>0'),
    ('(revision>0) AND (status IS NOT NULL)', '(revision>0) AND (status IS NOT NULL)'),
    ('((revision>0) OR (revision<10))', '(revision>0) OR (revision<10)'),
    ('((length(trim(content))>0))', 'length(trim(content))>0'),
    ('''(("revision" > 0 AND label != 'it''s (CHECK(fake))'))''',
     '''revision > 0 AND label != 'it''s (CHECK(fake))' '''.strip()),
    ('((`escaped``(name)` IS NOT NULL AND [bracket(name)] IS NOT NULL))',
     '`escaped``(name)` IS NOT NULL AND [bracket(name)] IS NOT NULL'),
    ('(/* (ignored) */ revision > 0 -- ignored )\n)', 'revision>0'),
))
def test_expression_normalization_preserves_structure(expression, expected):
    assert _expression(_tokens(expression)) == _normalized(_tokens(expected))


def test_parentheses_not_enclosing_entire_expression_are_retained():
    tokens = _tokens('(revision)>0')
    assert _expression(tokens) == tokens
    assert _expression(tokens) != _expression(_tokens('revision>0'))


@pytest.mark.parametrize('expression', (
    '', '()', '(())', '(', ')', '(revision>0', 'revision>0)',
    ')(revision>0)(', '(revision>0)) OR (status IS NULL',
))
def test_expression_rejects_empty_or_unbalanced_tokens(expression):
    with pytest.raises(AssistantMemorySchemaError):
        _expression(_tokens(expression))


@pytest.mark.parametrize('expression', (
    "('open)", '("open)', '(`open)', '([open)', '(revision>0 /* open)',
))
def test_expression_pipeline_rejects_unclosed_quotes_or_comments(expression):
    with pytest.raises(AssistantMemorySchemaError):
        _expression(_tokens(expression))


@pytest.mark.parametrize('depth', (10_000, 20_000))
def test_deep_parentheses_normalize_without_recursion(depth):
    expression = '(' * depth + 'revision>0' + ')' * depth
    assert _expression(_tokens(expression)) == _expression(_tokens('revision>0'))
    assert _checks('CHECK(' + expression + ')') == _checks('CHECK(revision>0)')


class CountingTokens(Sequence):
    """Test-only access/copy counter, retained by slices to catch shrinking copies.

    A slice counts every copied position and shares counters with its parent.
    Thus reintroducing repeated shrinking slices cannot evade the work bound.
    No timer or diagnostic switch is needed in production.
    """

    def __init__(self, tokens, *, counters=None, offset=0):
        self.tokens = tokens
        self.offset = offset
        self.counters = counters if counters is not None else {
            'visits': [0] * len(tokens), 'slices': 0,
        }

    def __len__(self):
        return len(self.tokens)

    def __iter__(self):
        for index in range(len(self)):
            yield self[index]

    def __getitem__(self, key):
        if isinstance(key, slice):
            start, stop, step = key.indices(len(self))
            assert step == 1
            self.counters['slices'] += 1
            for index in range(start, stop):
                self.counters['visits'][self.offset + index] += 1
            return CountingTokens(self.tokens[key], counters=self.counters,
                                  offset=self.offset + start)
        index = key if key >= 0 else len(self) + key
        self.counters['visits'][self.offset + index] += 1
        return self.tokens[key]


@pytest.mark.parametrize('shape', ('nested', 'siblings'))
def test_expression_has_linear_token_visits_and_one_final_slice(shape):
    measurements = []
    for size in (1_000, 2_000):
        expression = ('(' * size + 'revision>0' + ')' * size if shape == 'nested'
                      else ' OR '.join('(revision>0)' for _ in range(size)))
        raw = _tokens(expression)
        counted = CountingTokens(raw)
        assert _expression(counted) == _expression(raw)
        visits = counted.counters['visits']
        assert counted.counters['slices'] == 1
        assert max(visits) <= 4
        assert sum(visits) <= 4 * len(raw)
        measurements.append(sum(visits))
    # Deterministic work counts, not a wall-clock microbenchmark: doubling
    # input doubles work, with a small fixed boundary overhead.
    assert 1.9 <= measurements[1] / measurements[0] <= 2.1
