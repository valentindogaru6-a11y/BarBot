"""Server-owned mutation language facts, never a provider output contract."""

from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


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
    UNICODE = 'suspicious_unicode'
    TOKEN_ROLE = 'ambiguous_token_role'


class UnicodeFlag(StrEnum):
    SPACE = 'space'
    UNICODE_SPACE = 'unicode_space'
    COMBINING = 'combining_mark'
    FORMAT = 'format_control'
    CONTROL = 'control_character'
    JOINER = 'zero_width_joiner'
    BIDI = 'bidi_control'
    VARIATION = 'variation_selector'
    MIXED_SCRIPT = 'mixed_script'
    HOMOGLYPH = 'confusable_keyword'
    BOUNDARY = 'keyword_boundary_manipulation'
    LENGTH = 'input_too_long'
    IGNORABLE = 'default_ignorable'


class UnicodeInspection(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    normalized_text: str = Field(repr=False, exclude=True, max_length=4000)
    detection_text: str = Field(repr=False, exclude=True, max_length=4000)
    flags: tuple[UnicodeFlag, ...] = ()
    positions: tuple[int, ...] = Field(default=(), max_length=32)
    forbidden: bool = False


class MutationTokenKind(StrEnum):
    ACTION = 'action'
    ENTITY = 'entity'
    QUANTITY = 'quantity'
    UNIT = 'unit'
    CANCEL = 'cancel'
    CORRECTION = 'correction'
    REASON = 'reason'
    BOUNDARY = 'boundary'
    UNKNOWN = 'unknown'
    DESTINATION = 'destination'
    SOURCE = 'source'


class MutationToken(BaseModel):
    """Ephemeral lexical metadata, not a replacement for persistent line facts."""
    model_config = ConfigDict(extra='forbid', frozen=True)
    normalized_value: str = Field(repr=False, exclude=True, max_length=4000)
    kind: MutationTokenKind
    clause_index: int = Field(ge=0, le=63)
    start: int = Field(ge=0, le=4000)
    end: int = Field(ge=0, le=4000)
    suspicious_unicode: bool = False


class MutationClauseKind(StrEnum):
    ACTION = 'action'
    ARGUMENTS = 'arguments'
    COMMENT = 'comment_reason'
    CORRECTION = 'correction'
    CANCELLATION = 'cancellation'
    CONTRADICTION = 'contradiction'
    UNKNOWN = 'unknown'


class MutationClause(BaseModel):
    """Local source spans only: no raw text or provider-owned classification."""
    model_config = ConfigDict(extra='forbid', frozen=True)
    kind: MutationClauseKind
    start: int = Field(ge=0, le=4000)
    end: int = Field(ge=0, le=4000)
    superseded: bool = False

    @model_validator(mode='after')
    def ordered_span(self):
        if self.end < self.start:
            raise ValueError('invalid clause span')
        return self


class MutationSpan(BaseModel):
    """Offsets in the bounded, Unicode-inspected source, never diagnostic text."""
    model_config = ConfigDict(extra='forbid', frozen=True)
    start: int = Field(ge=0, le=4000)
    end: int = Field(ge=0, le=4000)

    @model_validator(mode='after')
    def ordered(self):
        if self.end < self.start:
            raise ValueError('invalid mutation span')
        return self


class MutationCommand(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    action: Literal['writeoff', 'transfer']
    clause_index: int = Field(ge=0, le=63)
    action_span: MutationSpan
    body_span: MutationSpan


class MutationSyntax(BaseModel):
    """Ephemeral AST. Persistent storage keeps only validated mutation facts."""
    model_config = ConfigDict(extra='forbid', frozen=True)
    normalized_text: str = Field(repr=False, exclude=True, max_length=4000)
    commands: tuple[MutationCommand, ...] = Field(default=(), max_length=64)
    clauses: tuple[MutationClause, ...] = Field(default=(), max_length=64)
    tokens: tuple[MutationToken, ...] = Field(default=(), max_length=512, repr=False)
    entity_spans: tuple[MutationSpan, ...] = Field(default=(), max_length=512)
    quantity_spans: tuple[MutationSpan, ...] = Field(default=(), max_length=512)
    unit_spans: tuple[MutationSpan, ...] = Field(default=(), max_length=512)
    destination_spans: tuple[MutationSpan, ...] = Field(default=(), max_length=512)
    source_spans: tuple[MutationSpan, ...] = Field(default=(), max_length=512)
    correction_spans: tuple[MutationSpan, ...] = Field(default=(), max_length=64)
    comment_spans: tuple[MutationSpan, ...] = Field(default=(), max_length=64)
    disposition: MutationDisposition | None = None
    validation_category: MutationError | None = None
    restart_at: int | None = Field(default=None, ge=0, le=4000)


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
    clauses: tuple[MutationClause, ...] = Field(default=(), max_length=64)

    @property
    def allowed(self) -> bool:
        return bool(self.action and self.imperative_evidence and not self.explicit_cancellation
            and self.disposition in (MutationDisposition.PROCEED, MutationDisposition.CORRECTED)
            and self.error in (None,MutationError.PRODUCT,MutationError.QUANTITY,
                MutationError.MISSING_UNIT,MutationError.DESTINATION)
            and not any(not clause.superseded and clause.kind in (
                MutationClauseKind.CANCELLATION, MutationClauseKind.CONTRADICTION,
                MutationClauseKind.UNKNOWN) for clause in self.clauses)
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
