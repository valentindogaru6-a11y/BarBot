"""Server-owned mutation language facts, never a provider output contract."""

from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class MutationDisposition(StrEnum):
    PROCEED = 'proceed'
    CANCEL = 'cancel'
    CORRECTED = 'corrected'
    AMBIGUOUS = 'ambiguous'
    INVALID = 'invalid'


class MutationError(StrEnum):
    CANCELLED = 'cancelled'
    INVALID_QUANTITY = 'invalid_quantity'
    NON_POSITIVE = 'non_positive_quantity'
    PRECISION = 'excess_precision'
    OVERFLOW = 'quantity_overflow'
    UNIT = 'unsupported_unit'
    INCOMPATIBLE_UNIT = 'incompatible_unit'
    AMBIGUOUS = 'ambiguous_correction'
    RANGE = 'ambiguous_quantity'
    PRODUCT = 'missing_product'
    QUANTITY = 'missing_quantity'
    MISSING_UNIT = 'missing_unit'
    DESTINATION = 'missing_destination'
    SYNTAX = 'invalid_mutation_syntax'


class MutationLine(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    entity: str = Field(default='', max_length=200)
    quantity: Decimal | None = None
    unit: str = Field(default='', max_length=30)
    destination: str = Field(default='', max_length=200)
    comment: str = Field(default='', max_length=500)

    @field_validator('quantity', mode='before')
    @classmethod
    def no_float(cls, value):
        if isinstance(value, (float, bool)):
            raise ValueError('float quantity forbidden')
        return value

    @field_validator('quantity')
    @classmethod
    def positive(cls, value):
        if value is not None and (not value.is_finite() or value <= 0):
            raise ValueError('invalid quantity')
        return value


class ParsedMutation(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    action: Literal['writeoff', 'transfer'] | None = None
    lines: tuple[MutationLine, ...] = Field(default=(), max_length=20)
    disposition: MutationDisposition = MutationDisposition.PROCEED
    error: MutationError | None = None
    imperative_evidence: bool = False
    explicit_cancellation: bool = False
    correction_segment: str | None = Field(default=None, max_length=2000)

    @property
    def allowed(self) -> bool:
        return bool(self.action and self.imperative_evidence and not self.explicit_cancellation
            and self.disposition in (MutationDisposition.PROCEED, MutationDisposition.CORRECTED)
            and self.error in (None,MutationError.PRODUCT,MutationError.QUANTITY,
                MutationError.MISSING_UNIT,MutationError.DESTINATION)
            and self.lines and all(line.entity for line in self.lines))

    @property
    def missing(self) -> tuple[str, ...]:
        return tuple(name for name, absent in (
            ('product', any(not line.entity for line in self.lines)),
            ('quantity', any(line.quantity is None for line in self.lines)),
            ('unit', any(line.quantity is not None and not line.unit for line in self.lines)),
            ('destination', self.action == 'transfer' and any(not line.destination for line in self.lines)),
        ) if absent)

    def command(self) -> str:
        """Render once for the existing intake boundary; do not re-infer facts."""
        header = 'списание:' if self.action == 'writeoff' else 'перемещение:'
        rows = []
        for line in self.lines:
            row = line.entity
            if line.quantity is not None:
                row += ' ' + format(line.quantity, 'f')
            if line.unit:
                row += ' ' + line.unit
            if line.destination:
                row += ' -> ' + line.destination
            if line.comment:
                row += ' (' + line.comment.replace('(', '[').replace(')', ']') + ')'
            rows.append(row)
        return header + '\n' + '\n'.join(rows)
