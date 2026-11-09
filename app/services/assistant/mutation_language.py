"""Bounded RU/UA action/cancellation/correction grammar with whole-token numbers.

This module has no storage, provider or permission authority. A valid result
still needs the normal access, product/destination resolution and confirmation.
"""

import re
import unicodedata
from decimal import Decimal, localcontext

from app.models.assistant_mutation import (
    MutationDisposition as D, MutationError as E, MutationLine, ParsedMutation,
)
from app.services.warehouse.quantity_service import QuantityError, parse_quantity


_WRITEOFF = re.compile(r'^(?:пожалуйста\s+)?(?:спиши(?:те)?|списать|нужно\s+списать|'
    r'(?:убери(?:те)?|добавь(?:те)?)\s+в\s+списание|(?:сделай|оформи)\s+списани[ея]|'
    r'списание|спиши|списуй|списати|зроби\s+списання|списання)\b\s*:?\s*')
_TRANSFER = re.compile(r'^(?:пожалуйста\s+)?(?:перемести(?:те)?|переместить|перенеси(?:те)?|'
    r'перенести|отправь(?:те)?|(?:сделай|оформи)\s+перемещени[ея]|перемещение|'
    r'перемисти|перенеси|зроби\s+перемищення|перемищення)\b\s*:?\s*')
_DOCUMENT_SEND = re.compile(r'\b(?:отчет\w*|табел\w*|xlsx|excel|файл\w*|документ\w*|списания)\b')
_NEGATED_ACTION = re.compile(r'\bне\s+(?:(?:надо|нужно|треба|потрібно|потрибно)\s+)?'
    r'(?:(?:ничего|ничого|больше|бильше)\s+)?(?:списывай\w*|списывать|списуй\w*|списувати|'
    r'спиши\w*|делай\w*|делать|роби\w*|перемещай\w*|перемещать|переноси\w*|'
    r'перемищуй\w*|отправляй\w*|надсилай\w*)\b')
_CANCEL = re.compile(r'^(?:(?:нет|ни)\s*[,!:;]?\s*)?(?:отмена|отмени|отбой|стоп|'
    r'скасуй|скасування|видмина|передумал[а]?|передумав|передумала|хотя\s+нет|'
    r'не\s+(?:надо|нужно|треба))\s*[.!?]*$')
_CANCEL_TAIL = re.compile(r'(?:^|\s)' + _CANCEL.pattern[1:])
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


def _text(value):
    # Keep numeric signs, separators and line boundaries intact.
    return unicodedata.normalize('NFKC', value).casefold().replace('ё','е').replace('і','и').strip()


def explicit_cancel(value: str, *, contextual=False) -> bool:
    text = _text(value)
    if text in ('нет','ни'):
        return True
    if _NEGATED_ACTION.search(text):
        return True
    if (contextual or _action(text)[0] or _GENERIC_IMPERATIVE.match(text)) and _CANCEL_TAIL.search(text):
        return True
    clauses = re.split(r'[,;\n]+', text)
    return (len(clauses)>1 and clauses[-1].strip(' .!?') in ('нет','ни')) or any(
        _CANCEL.fullmatch(clause.strip()) for clause in clauses) or bool(
        re.search(r'\b(?:хотя\s+нет|передумал[а]?|передумав|передумала)\s*[.!?]*$', text))


def _action(text):
    for name, pattern in (('writeoff', _WRITEOFF), ('transfer', _TRANSFER)):
        if match := pattern.match(text):
            if name == 'transfer' and re.match(r'^(?:пожалуйста\s+)?отправь',text) and _DOCUMENT_SEND.search(text):
                # Report delivery has its own capability/FSM; "send" alone
                # must not steal that route as a warehouse product transfer.
                return None, text
            return name, text[match.end():]
    return None, text


def _failure(action, disposition, error, *, imperative=True):
    return ParsedMutation(action=action, disposition=disposition, error=error,
        imperative_evidence=imperative)


def _quantity_tokens(core):
    """Every number-like whitespace token is consumed completely, never searched."""
    words = core.split()
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
            kept.append(token.strip(',;.!?'))
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
        measures = [word for word in kept if word in _UNITS]
        if len(measures) > 1:
            return None, '', E.RANGE
        if measures:
            unit = measures[0]
            kept.remove(unit)
            # Existing UX: a spelled singular measure explicitly means one.
            number = Decimal('1') if kept and unit in ('килограмм','литр','штука') else None
            found.append((number,unit))
    return found[0] if found else (None, ''), ' '.join(kept), None


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


def parse_mutation(value: str, *, previous: ParsedMutation | None = None) -> ParsedMutation | None:
    """One immutable analysis shared by foreground, planner and correction paths."""
    text = _text(value)
    action, body = _action(text)
    if len(text) > 4000:
        return _failure(action, D.INVALID, E.SYNTAX, imperative=action is not None)
    if explicit_cancel(text,contextual=previous is not None):
        return ParsedMutation(action=action, disposition=D.CANCEL, error=E.CANCELLED,
            imperative_evidence=action is not None, explicit_cancellation=True)
    inherited = False
    if action is None:
        if previous and previous.allowed and len(previous.lines) == 1 and (
            re.search(r'[0-9]', text) or text.split(' ',1)[0] in _SPECIAL or
            text.split(' ',1)[0] in _UNITS or
            (previous.missing==('unit',) and len(text.split())==1) or
            re.match(r'^(?:в|на|->|→|нет|точнее|исправь|виправ)\b', text)):
            action, body, inherited = previous.action, text, True
            if previous.missing==('unit',) and len(text.split())==1 and not re.search(r'[0-9]',text) and text not in _UNITS:
                return _failure(action,D.INVALID,E.UNIT)
        else:
            return None
    lines = []
    corrected = None
    for raw in body.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        other_action, raw_body = _action(raw)
        if other_action and other_action != action:
            return _failure(action, D.AMBIGUOUS, E.AMBIGUOUS)
        line, error, correction = _corrected_line(raw_body, action,
            previous.lines[0] if inherited else None)
        if error:
            return _failure(action, D.AMBIGUOUS if error in (E.RANGE,E.AMBIGUOUS) else D.INVALID, error)
        lines.append(line)
        corrected = correction or corrected
        if len(lines) > 20:
            return _failure(action, D.INVALID, E.SYNTAX)
    if not lines:
        return _failure(action, D.AMBIGUOUS, E.PRODUCT)
    if corrected is not None:
        corrected = ' '.join(corrected.split())
        if len(corrected) > 2000:
            return _failure(action,D.INVALID,E.SYNTAX)
    result = ParsedMutation(action=action, lines=tuple(lines), imperative_evidence=True,
        disposition=D.CORRECTED if corrected else D.PROCEED, correction_segment=corrected)
    missing = result.missing
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
