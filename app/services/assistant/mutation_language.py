"""Bounded RU/UA action/cancellation/correction grammar with whole-token numbers.

This module has no storage, provider or permission authority. A valid result
still needs the normal access, product/destination resolution and confirmation.
"""

import re
from contextvars import ContextVar
from functools import wraps
from dataclasses import replace
from decimal import Decimal, localcontext

from app.models.assistant_mutation import (
    MutationClauseKind as K, MutationDisposition as D, MutationError as E, MutationLine, ParsedMutation,
    MutationSyntax, MutationSpan, MutationTokenKind as T,
)
from app.services.assistant.mutation_clauses import analyze_clauses
from app.services.assistant.mutation_clauses import ClauseAnalysis
from app.services.assistant.mutation_tokens import inspect_unicode, normalize_command
from app.services.warehouse.quantity_service import QuantityError, parse_quantity


_WRITEOFF = re.compile(r'^(?:пожалуйста\s+)?(?:спиши(?:те)?|списать|нужно\s+списать|'
    r'(?:убери(?:те)?|добавь(?:те)?)\s+в\s+списание|(?:сделай|оформи)\s+списани[ея]|'
    r'списание|спиши|списуй|списати|зроби\s+списання|списання)\b\s*:?\s*')
_TRANSFER = re.compile(r'^(?:пожалуйста\s+)?(?:перемести(?:те)?|переместить|перенеси(?:те)?|'
    r'перенести|отправь(?:те)?|(?:сделай|оформи)\s+перемещени[ея]|перемещение|'
    r'перемисти|перенеси|зроби\s+перемищення|перемищення)\b\s*:?\s*')
_DOCUMENT_SEND = re.compile(r'\b(?:отчет\w*|табел\w*|xlsx|excel|файл\w*|документ\w*|списания)\b')
_GENERIC_IMPERATIVE = re.compile(r'^(?:пожалуйста\s+)?(?:измени|изменить|добавь|добавить|'
    r'удали|удалить|убери|замени|заменить|закрой|закрыть|открой|открыть|создай|создать|'
    r'отправь|отправить|настрой|настроить|отмени|отменить|подтверди|подтвердить)\b')
_CORRECTION = re.compile(r'(?:^|[,;]\s*|\s+)(?:нет\s*[,;:]\s*|точнее\s*[,;:]?\s*|'
    r'точнише\s*[,;:]?\s*|исправь\s+на\s+|виправ\s+на\s+)')
_FIELD_CORRECTION = re.compile(r'(?:^|[,;]\s*)(?:исправь|замени|виправ)\s+'
    r'(?P<field>продукт|товар|направление|напрямок)\s+на\s+(?P<value>.+)$')
_CONTRAST = re.compile(r'\bне\s+(.+?)\s*,?\s+а\s+')
_NUMBER = r'(?:[0-9]+(?:[.,][0-9]+)?|[.,][0-9]+)'
_UNITS = {
    **dict.fromkeys(('кг','kg','килограмм','килограмма','килограммов'), 'kg'),
    **dict.fromkeys(('г','гр','g','грамм','грамма','граммов','грамів','грамов'), 'kg'),
    **dict.fromkeys(('л','l','литр','литра','литров'), 'l'),
    **dict.fromkeys(('мл','ml','миллилитр','миллилитра','миллилитров'), 'l'),
    **dict.fromkeys(('шт','шт.','pcs','штука','штуки','штук'), 'pcs'),
}
_ATTACHED = re.compile(rf'(?P<number>{_NUMBER})(?P<unit>[^0-9\W][\w.]*)?$', re.UNICODE)
_DEST = re.compile(r'(?:->|→|\b(?:в|на)\s+)\s*(.+)$')
_RANGE = re.compile(r'[0-9]\s*[-–—/]\s*[0-9]|\b(?:или|або|примерно|приблизно|может|можливо)\b')
_SPECIAL = frozenset(('nan','inf','infinity','infinite','∞'))
_BOUND_FACTS: ContextVar[dict[str, ParsedMutation] | None] = ContextVar('server_mutation_facts',default=None)


def isolated_mutation_facts(function):
    """Request-local facts only; no actor/chat IDs or durable extra state."""
    @wraps(function)
    async def scoped(*args,**kwargs):
        token = _BOUND_FACTS.set({})
        try:
            return await function(*args,**kwargs)
        finally:
            _BOUND_FACTS.reset(token)
    return scoped


def bind_mutation_facts(parsed: ParsedMutation) -> None:
    """Called by the handler only after local syntax/access/product preflight.

    Legacy interfaces still accept rendered intake. Their parser can retrieve
    the immutable facts for that exact rendering, never infer another command
    from an already typed product. Any changed text takes the ordinary gate.
    """
    scope = _BOUND_FACTS.get()
    if scope is None or not parsed.allowed or parsed.missing:
        raise ValueError('unvalidated mutation binding')
    scope.clear()
    scope[_text(parsed.command())] = parsed


def _text(value):
    # Keep numeric signs, separators and line boundaries intact.
    return normalize_command(value)


def explicit_cancel(value: str, *, contextual=False) -> bool:
    return cancellation_analysis(value, contextual=contextual).disposition == D.CANCEL


def cancellation_analysis(value: str, *, contextual=False, exact_entities=()):
    """Shared veto for the AI and active warehouse FSM (never confirmation)."""
    inspection = inspect_unicode(value)
    if inspection.forbidden:
        return ClauseAnalysis((),D.INVALID,inspection=inspection)
    text = inspection.normalized_text
    result = analyze_clauses(text, action=_action, units=frozenset(_UNITS),
        exact_entities=frozenset(_text(name) for name in exact_entities),
        active=bool(_GENERIC_IMPERATIVE.match(text)), contextual=contextual)
    return replace(result,inspection=inspection)


def mutation_syntax(value: str, *, contextual=False, exact_entities=()) -> MutationSyntax:
    """One clause-level action pass; downstream code only consumes its spans."""
    analysis = cancellation_analysis(value,contextual=contextual,exact_entities=exact_entities)
    comments = {index for index,c in enumerate(analysis.clauses) if c.kind == K.COMMENT}
    def spans(kind):
        return tuple(MutationSpan(start=t.start,end=t.end) for t in analysis.tokens
            if t.kind == kind and t.clause_index not in comments)
    error = (E.UNICODE if analysis.inspection.forbidden else
        E.CANCELLED if analysis.disposition == D.CANCEL else
        E.TOKEN_ROLE if analysis.ambiguous_entity else
        E.AMBIGUOUS if analysis.disposition is not None else None)
    return MutationSyntax(normalized_text=analysis.inspection.normalized_text,
        commands=analysis.commands,clauses=analysis.clauses,tokens=analysis.tokens,
        entity_spans=spans(T.ENTITY),quantity_spans=spans(T.QUANTITY),unit_spans=spans(T.UNIT),
        destination_spans=spans(T.DESTINATION),source_spans=spans(T.SOURCE),
        correction_spans=tuple(MutationSpan(start=c.start,end=c.end) for c in analysis.clauses if c.kind == K.CORRECTION),
        comment_spans=tuple(MutationSpan(start=c.start,end=c.end) for c in analysis.clauses if c.kind == K.COMMENT),
        disposition=analysis.disposition,validation_category=error,restart_at=analysis.restart_at)


def _action(text):
    # Only this closed positive imperative template may strip a negation.
    text = re.sub(r'^не\s+забудь\s+(?=(?:списать|переместить)\b)', '', text)
    for name, pattern in (('writeoff', _WRITEOFF), ('transfer', _TRANSFER)):
        if match := pattern.match(text):
            if name == 'transfer' and re.match(r'^(?:пожалуйста\s+)?отправь',text) and _DOCUMENT_SEND.search(text):
                # Report delivery has its own capability/FSM; "send" alone
                # must not steal that route as a warehouse product transfer.
                return None, text
            return name, text[match.end():]
    return None, text


def _failure(action, disposition, error, *, imperative=True, clauses=()):
    return ParsedMutation(action=action, disposition=disposition, error=error,
        imperative_evidence=imperative, clauses=clauses)


def _quantity_tokens(core):
    """Every number-like whitespace token is consumed completely, never searched."""
    positions = [MutationSpan(start=m.start(),end=m.end()) for m in re.finditer(r'\S+',core)]
    words = [core[s.start:s.end] for s in positions]
    for index, token in enumerate(words):
        if token.endswith(('.',',',';','!','?')):
            # Punctuation after an explicit measure is safe. A bare "1." or
            # "1," remains an invalid incomplete decimal, never a prefix 1.
            candidate = token[:-1]
            attached = _ATTACHED.fullmatch(candidate)
            if candidate in _UNITS or (attached and attached.group('unit') in _UNITS):
                words[index] = candidate
    found = []
    kept = []
    index = 0
    while index < len(words):
        token = words[index]
        numeric = bool(re.search(r'[0-9]', token)) and (token[0].isdigit() or token[0] in '+-−–—.,')
        if token in _SPECIAL:
            return None, '', E.INVALID_QUANTITY
        if not numeric:
            kept.append(positions[index])
            index += 1
            continue
        # Only conventional 3-digit grouping; never reinterpret arbitrary pairs.
        if re.fullmatch(r'[0-9]{1,3}', token):
            while index + 1 < len(words) and re.fullmatch(r'[0-9]{3}(?:[^0-9\W][\w.]*)?', words[index+1]):
                index += 1
                token += words[index]
        if re.match(r'^[-−–—+]', token):
            return None, '', E.INVALID_QUANTITY
        if _RANGE.search(token):
            return None, '', E.RANGE
        match = _ATTACHED.fullmatch(token)
        if not match:
            return None, '', E.INVALID_QUANTITY
        number, unit = match.group('number'), match.group('unit') or ''
        if not unit and index + 1 < len(words) and words[index+1] in _UNITS:
            index += 1
            unit = words[index]
        if unit and unit not in _UNITS:
            return None, '', E.UNIT
        if len(number) > 50:
            return None, '', E.OVERFLOW
        with localcontext() as context:
            context.prec = 80
            try:
                parsed = parse_quantity(number, unit or None, _UNITS.get(unit, 'kg'))
            except QuantityError as exc:
                return None, '', E(exc.code)
        found.append((Decimal(parsed.input_quantity_text.replace(',', '.')), unit))
        index += 1
    if len(found) > 1:
        return None, '', E.RANGE
    if not found:
        measures = [span for span in kept if core[span.start:span.end].strip(',;.!?') in _UNITS]
        if len(measures) > 1:
            return None, '', E.RANGE
        if measures:
            measure = measures[0]
            unit = core[measure.start:measure.end].strip(',;.!?')
            kept.remove(measure)
            # Existing UX: a spelled singular measure explicitly means one.
            number = Decimal('1') if kept and unit in ('килограмм','литр','штука') else None
            found.append((number,unit))
    entity = ' '.join(core[s.start:s.end].strip(',;.!?') for s in kept)
    return found[0] if found else (None, ''), entity, None


def _line(core, action, previous=None):
    comment = ''
    if '(' in core or ')' in core:
        match = re.search(r'\(([^()]*)\)\s*$', core)
        if not match or not match.group(1).strip() or '(' in core[:match.start()] or ')' in core[:match.start()]:
            return None, E.SYNTAX
        comment, core = ' '.join(match.group(1).split()), core[:match.start()].strip()
    # A prose comment starts after a delimiter, not after a decimal separator.
    match = re.search(r'(?<=[^\W\d_]),\s+(?=[^\W\d_])', core)
    if match:
        comment, core = ' '.join(core[match.end():].split()), core[:match.start()].strip()
        if re.search(r'[0-9]',comment) and not re.match(r'^(?:потому\s+что|так\s+как|причина\b)',comment):
            # A second amount cannot silently become a prose comment. Numeric
            # comments remain explicit in parentheses or a reason clause.
            return None, E.AMBIGUOUS
    if len(comment) > 500:
        return None, E.SYNTAX
    if re.search(r'(?<!\w)[+−–—-]\s*[.,]?[0-9]', core):
        return None, E.INVALID_QUANTITY
    if _RANGE.search(core):
        return None, E.RANGE
    destination = ''
    if action == 'transfer' and (match := _DEST.search(core)):
        destination = ' '.join(match.group(1).split()).rstrip('.!?;')
        core = core[:match.start()].strip()
        if destination.endswith('ю'):
            destination = destination[:-1] + 'я'
        elif destination.endswith('у'):
            destination = destination[:-1] + 'а'
    quantity, entity, error = _quantity_tokens(core)
    if error:
        return None, error
    number, unit = quantity
    if previous is not None:
        if entity and entity != previous.entity:
            return None, E.AMBIGUOUS
        if unit and previous.unit and _UNITS[unit] != _UNITS[previous.unit]:
            return None, E.INCOMPATIBLE_UNIT
        entity = entity or previous.entity
        if number is None:
            number = previous.quantity
            if number is None and unit in ('килограмм','литр','штука'):
                number = Decimal('1')
        unit = unit or previous.unit
        destination = destination or previous.destination
        comment = comment or previous.comment
    if len(entity) > 200 or len(destination) > 200:
        return None, E.SYNTAX
    return MutationLine(entity=entity, quantity=number, unit=unit,
        destination=destination, comment=comment), None


def _corrected_line(body, action, previous=None):
    correction = None
    if field := _FIELD_CORRECTION.search(body):
        base, error = _line(body[:field.start()].strip(' ,;'), action, previous)
        value = field.group('value').strip()
        if error or not base or not re.fullmatch(r'[^0-9,;()<>\n]{1,200}', value) or _RANGE.search(value):
            return None, error or E.AMBIGUOUS, None
        if field.group('field') in ('продукт','товар'):
            base = MutationLine.model_validate({**base.model_dump(),'entity':value})
        elif action == 'transfer':
            base, error = _line('на '+value,action,base)
        else:
            return None, E.AMBIGUOUS, None
        return base, error, body[field.start():].strip()
    contrast = _CONTRAST.search(body)
    old_unit = ''
    if contrast:
        old = contrast.group(1).strip(' ,;')
        # The contrast must actually replace a numeric value, not a product.
        if not re.search(r'[0-9]', old):
            return None, E.AMBIGUOUS, None
        old_quantity, _, old_error = _quantity_tokens(old)
        if old_error:
            return None, old_error, None
        old_unit = old_quantity[1]
        correction = body[contrast.end():].strip()
        body = body[:contrast.start()] + correction
    matches = list(_CORRECTION.finditer(body))
    if matches:
        start = 0
        base = previous
        for match in matches:
            base, error = _line(body[start:match.start()].strip(' ,;'), action, base)
            if error:
                return None, error, None
            start = match.end()
        correction = body[start:].strip()
        if not correction:
            return None, E.AMBIGUOUS, None
        line, error = _line(correction, action, base)
    else:
        line, error = _line(body, action, previous)
    if re.search(r'\b(?:нет|ни)\b', body) and not matches:
        return None, E.AMBIGUOUS, None
    if line and old_unit and line.unit and _UNITS[old_unit] != _UNITS[line.unit]:
        return None, E.INCOMPATIBLE_UNIT, None
    return line, error, correction


def parse_mutation(value: str, *, previous: ParsedMutation | None = None, exact_entities=()) -> ParsedMutation | None:
    """One immutable analysis shared by foreground, planner and correction paths."""
    inspection = inspect_unicode(value)
    if inspection.forbidden:
        return _failure(None,D.INVALID,E.UNICODE,imperative=False)
    scope = _BOUND_FACTS.get()
    if scope is not None and (bound := scope.get(inspection.normalized_text)) is not None:
        return bound
    analysis = mutation_syntax(value,contextual=previous is not None,exact_entities=exact_entities)
    if analysis.validation_category == E.UNICODE:
        return _failure(None,D.INVALID,E.UNICODE,imperative=False)
    text = analysis.normalized_text
    command = analysis.commands[0] if analysis.commands else None
    action = command.action if command else None
    body_start = command.body_span.start if command else 0
    body = text[body_start:]
    if analysis.disposition is not None:
        return ParsedMutation(action=action, disposition=analysis.disposition,
            error=analysis.validation_category,
            imperative_evidence=action is not None,
            explicit_cancellation=any(clause.kind == K.CANCELLATION for clause in analysis.clauses),
            clauses=analysis.clauses)
    restart = analysis.restart_at
    if restart is not None:
        # Only a fully new explicit action can supersede a cancellation. No
        # partial quantity/correction inherits the cancelled product or amount.
        command = next((c for c in analysis.commands if c.action_span.start == restart),None)
        action = command.action if command else None
        body_start = command.body_span.start if command else restart
        body = text[body_start:]
        previous = None
        if action is None or len(text[restart:].strip()) > 2000:
            return _failure(action,D.AMBIGUOUS,E.AMBIGUOUS,clauses=analysis.clauses)
    inherited = False
    if action is None:
        if previous and previous.allowed and len(previous.lines) == 1 and (
            re.search(r'[0-9]', text) or text.split(' ',1)[0] in _SPECIAL or
            text.split(' ',1)[0] in _UNITS or
            (previous.missing==('unit',) and len(text.split())==1) or
            re.match(r'^(?:в|на|->|→|нет|точнее|исправь|виправ)\b', text)):
            # Prior releases could persist controls in an otherwise typed
            # product/comment. A unit-only reply must not revive that input.
            if any(inspect_unicode(field).forbidden for line in previous.lines
                for field in (line.entity,line.unit,line.destination,line.comment)):
                return _failure(previous.action,D.INVALID,E.UNICODE,imperative=False,
                    clauses=analysis.clauses)
            if not previous.clauses and any(line.comment for line in previous.lines):
                # Legacy free-text tails lack clause evidence. Refuse only
                # resumption, not independent read-only requests or commands.
                return _failure(previous.action,D.AMBIGUOUS,E.AMBIGUOUS,clauses=analysis.clauses)
            action, body, inherited = previous.action, text, True
            if previous.missing==('unit',) and len(text.split())==1 and not re.search(r'[0-9]',text) and text not in _UNITS:
                return _failure(action,D.INVALID,E.UNIT,clauses=analysis.clauses)
        else:
            return None
    lines = []
    corrected = None
    offset = body_start
    for row in body.splitlines(keepends=True):
        row_start = offset + len(row)-len(row.lstrip())
        offset += len(row)
        raw = row.strip()
        if not raw:
            continue
        row_command = next((c for c in analysis.commands if c.action_span.start == row_start),None)
        if row_command and row_command.action != action:
            return _failure(action, D.AMBIGUOUS, E.AMBIGUOUS,clauses=analysis.clauses)
        raw_body = text[row_command.body_span.start:offset].strip() if row_command else raw
        line, error, correction = _corrected_line(raw_body, action,
            previous.lines[0] if inherited else None)
        if error:
            return _failure(action, D.AMBIGUOUS if error in (E.RANGE,E.AMBIGUOUS) else D.INVALID, error,clauses=analysis.clauses)
        lines.append(line)
        corrected = correction or corrected
        if len(lines) > 20:
            return _failure(action, D.INVALID, E.SYNTAX,clauses=analysis.clauses)
    if not lines:
        return _failure(action, D.AMBIGUOUS, E.PRODUCT,clauses=analysis.clauses)
    if corrected is not None:
        corrected = ' '.join(corrected.split())
        if len(corrected) > 2000:
            return _failure(action,D.INVALID,E.SYNTAX,clauses=analysis.clauses)
    result = ParsedMutation(action=action, lines=tuple(lines), imperative_evidence=True,
        disposition=D.CORRECTED if corrected or restart is not None else D.PROCEED,
        correction_segment=corrected or (text[restart:].strip() if restart is not None else None),
        clauses=tuple(clause.model_copy(update={'superseded':True})
            if restart is not None and clause.end <= restart else clause for clause in analysis.clauses))
    missing = result.missing
    if restart is not None and (missing or not result.allowed):
        return _failure(action,D.AMBIGUOUS,E.AMBIGUOUS,clauses=analysis.clauses)
    if missing:
        result = result.model_copy(update={'error': {'product':E.PRODUCT,'quantity':E.QUANTITY,
            'unit':E.MISSING_UNIT,'destination':E.DESTINATION}[missing[0]]})
    return result


def mutation_reply(result: ParsedMutation) -> str:
    """Closed diagnostics: never include the rejected text/quantity/provider data."""
    if result.disposition == D.CANCEL:
        return 'Действие отменено. Новые операции не созданы.'
    if result.disposition == D.AMBIGUOUS:
        return 'Уточните одну позицию и одно точное количество без диапазона или неоднозначного исправления.'
    if result.disposition == D.INVALID:
        return 'Некорректное количество, единица или формат строки. Введите позицию заново; черновик не создан.'
    labels = {'product':'продукт','quantity':'количество','unit':'единицу измерения','destination':'направление перемещения'}
    return 'Укажите ' + ' и '.join(labels[name] for name in result.missing) + '.'


def resolve_product_case(result: ParsedMutation, products) -> ParsedMutation:
    """Resolve an exact, unique grammatical case, never a fuzzy candidate.

    Existing canonical names/aliases take precedence. Unrecognised inflections
    and collisions continue through the ordinary explicit product resolution.
    """
    def genitive(name):
        words = name.split()
        output = []
        for index, word in enumerate(words):
            if word.endswith(('ое','ый')):
                word = word[:-2]+'ого'
            elif word.endswith('ая'):
                word = word[:-2]+'ой'
            elif word.endswith('ий'):
                word = word[:-2]+'его'
            elif index == len(words)-1:
                if word.endswith('о'):
                    word = word[:-1]+'а'
                elif word.endswith('а'):
                    word = word[:-1]+('и' if word[-2:-1] in 'гкхжчшщ' else 'ы')
                elif word.endswith('я'):
                    word = word[:-1]+'и'
                elif word.endswith('й'):
                    word = word[:-1]+'я'
                elif word and word[-1] in 'бвгджзклмнпрстфхцчшщ':
                    word += 'а'
            output.append(word)
        return ' '.join(output)
    variants = {}
    exact = set()
    for product in products:
        operation = 'product_writeoff' if result.action == 'writeoff' else result.action
        if operation not in product.operation_types:
            continue
        names = (product.normalized_name,*product.aliases)
        exact.update(_text(name) for name in names)
        variants.setdefault(genitive(_text(product.normalized_name)),[]).append(product)
    lines = []
    for line in result.lines:
        matches = variants.get(_text(line.entity),()) if _text(line.entity) not in exact else ()
        lines.append(line.model_copy(update={'entity': matches[0].official_name}) if len(matches)==1 else line)
    return ParsedMutation.model_validate({**result.model_dump(),'lines':tuple(lines)})
