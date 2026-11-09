"""Bounded, server-owned clause analysis; never a fuzzy product/action matcher.

Numbers are not rewritten here. The existing whole-token Decimal parser owns
arguments. Typo *recognition* is restricted to one edit of a closed cancellation
lexicon at a clause head/isolated suffix; it never repairs text or grants action.
"""

from dataclasses import dataclass
import re
from typing import Callable

from app.models.assistant_mutation import (
    MutationClause, MutationClauseKind as K, MutationDisposition as D,
)


_LEXEMES = frozenset(('отмена', 'отмени', 'отменить', 'отменяй', 'отбой', 'стоп',
    'скасуй', 'скасувати', 'скасування', 'видмина', 'передумал', 'передумала', 'передумав'))
_IMPERATIVES = frozenset(('отмени', 'отменить', 'отменяй', 'скасуй', 'скасувати'))
_CANCEL_NOUNS = frozenset(('отмена', 'отмену', 'отмены', 'скасування', 'видмина'))
_NEGATED_ACTION = re.compile(r'\bне\s+(?:(?:надо|нужно|треба|потрибно)\s+)?'
    r'(?:(?:ничего|ничого|больше|бильше)\s+)?(?:списывай\w*|списывать|списуй\w*|списувати|'
    r'спиши\w*|делай\w*|делать|роби\w*|перемещай\w*|перемещать|переноси\w*|'
    r'перемищуй\w*|отправляй\w*|надсилай\w*)\b')
_NEGATED_CANCEL = re.compile(r'\bне\s+(?:отмен\w*|скасов\w*|скасуй|скасувати)\b')
_ACTION_REF = re.compile(r'\b(?:списани[еяй]|списывать|списуй|списувати|списання|'
    r'перемещени[еяй]|перемищення|действи[ея]|запись)\b')
_CONNECTORS = re.compile(r'^(?:(?:а|и|но|ну|потом|затем|тогда|пожалуйста|будь ласка)\s+)+')
_REASON = re.compile(r'^(?:причина|комментарий|потому\s+что|так\s+как|бо|тому\s+що)\b\s*:?\s*')
_CORRECTION = re.compile(r'^(?:точнее|точнише|(?:исправь|виправ)\s+на|'
    r'(?:исправь|замени|виправ)\s+(?:продукт|товар|направление|напрямок)\s+на)\b')
_CORRECTION_WORD = re.compile(r'\b(?:точнее|точнише|исправь|замени|виправ)\b')
_DIALOGUE_CORRECTION = re.compile(r'^(?:я\s+)?(?:имел|имела|мав|мала)\s+(?:в\s+виду|на\s+увази)\b')
_TOKENS = re.compile(r'[^\W\d_]+', re.UNICODE)
_SEPARATORS = re.compile(r'[,;\n()]|[—–](?=\s)|(?<=\s)-(?=\s)|[.!?]')
_REFERENTS = frozenset(('это','его','ее','все','запись','действие','списание',
    'списания','перемещение','перемещения','пожалуйста','отмену','отмены'))
_DESCRIPTIVE_NEGATION = re.compile(r'^не\s+(?:свеж\w*|пригод\w*|использовать|распечат\w*)$')
_REASONS = frozenset(('испорчен', 'испорчено', 'испортился', 'испортилась', 'сгнил',
    'сгнила', 'сгнило', 'упал', 'упала', 'разбился', 'разбилась', 'пролил', 'пролилось',
    'нераспечатанный', 'неиспользованный', 'просрочен', 'просрочка'))


def _one_edit(left: str, right: str) -> bool:
    """Damerau distance <=1, linear in token length, no prefix matching."""
    if abs(len(left) - len(right)) > 1:
        return False
    if left == right:
        return True
    if len(left) == len(right):
        differences = [i for i, (a, b) in enumerate(zip(left, right)) if a != b]
        return len(differences) == 1 or (len(differences) == 2
            and differences[1] == differences[0] + 1
            and left[differences[0]] == right[differences[1]]
            and left[differences[1]] == right[differences[0]])
    short, long = (left, right) if len(left) < len(right) else (right, left)
    index = next((i for i, (a, b) in enumerate(zip(short, long)) if a != b), len(short))
    return short[index:] == long[index + 1:]


def _typos(token: str) -> tuple[str, ...]:
    if len(token) < 5 or len(token) > 20:
        return ()
    return tuple(sorted(word for word in _LEXEMES if len(word) >= 5 and _one_edit(token, word)))


def _spans(text):
    """Keep decimal punctuation and source offsets; at most 64 clauses."""
    start = 0
    parenthetical = False
    reason = False
    for separator in _SEPARATORS.finditer(text):
        pos = separator.start()
        if text[pos] in ',.' and pos + 1 < len(text) and text[pos+1].isdigit() and (
            not pos or text[pos-1].isdigit() or text[pos-1].isspace()):
            continue
        if start < pos and text[start:pos].strip():
            yield start, pos, parenthetical or reason
            reason = bool(_REASON.match(text[start:pos].strip()))
        parenthetical = text[pos] == '(' or (parenthetical and text[pos] != ')')
        start = separator.end()
    if text[start:].strip():
        yield start, len(text), parenthetical or reason


@dataclass(frozen=True)
class ClauseAnalysis:
    clauses: tuple[MutationClause, ...]
    disposition: D | None = None
    restart_at: int | None = None


def analyze_clauses(text: str, *, action: Callable[[str], bool], active=False,
                    contextual=False) -> ClauseAnalysis:
    """Text is normalized by the mutation parser; output has no raw values."""
    if len(text) > 4000:
        return ClauseAnalysis((), D.AMBIGUOUS)
    spans = []
    for span in _spans(text):
        spans.append(span)
        if len(spans) > 64:
            return ClauseAnalysis((), D.AMBIGUOUS)
    clauses = []
    cancellations = []
    contradictions = []
    actions = []
    for index, (start, end, reason_context) in enumerate(spans):
        raw = text[start:end].strip()
        head = _CONNECTORS.sub('', raw).strip()
        tokens = _TOKENS.findall(head)
        words = set(tokens)
        is_action = action(head)
        reason = bool(_REASON.match(raw)) or reason_context
        negative_action = bool(_NEGATED_ACTION.search(raw))
        # A direct command still vetoes a comment/reason marker. Nouns such as
        # "отмена поставки" alone are not cancellation of the current action.
        imperative = words & _IMPERATIVES
        if is_action:
            # A product may itself have a cancellation-like name. Only an
            # imperative after the argument amount vetoes this action clause.
            number = re.search(r'[0-9]',raw)
            imperative = {word for word in imperative if number and any(
                match.group() == word and match.start() > number.start()
                for match in _TOKENS.finditer(raw))}
        cancellation_words = [token for token in tokens if token in _LEXEMES | _CANCEL_NOUNS]
        contradiction = bool(_NEGATED_CANCEL.search(raw)) or (
            len(cancellation_words) > 1 and bool(imperative or _ACTION_REF.search(raw))) or (
            tokens.count('не') > 1 and (negative_action or _ACTION_REF.search(raw)))
        negated_typo = len(tokens) == 2 and tokens[0] == 'не' and bool(_typos(tokens[1]))
        reason_head = _REASON.sub('',head).strip()
        reason_tokens = _TOKENS.findall(reason_head)
        if contradiction or negated_typo:
            kind = K.CONTRADICTION
        elif negative_action:
            kind = K.CANCELLATION
        elif imperative:
            kind = K.CANCELLATION
        elif active and not reason and tokens and (tokens[-1] in _LEXEMES
            or tokens[-2:] in (['хотя','нет'], ['не','надо'], ['не','нужно'], ['не','треба'])):
            kind = K.CANCELLATION
        elif is_action:
            kind = K.ACTION
        elif head in ('нет', 'ни'):
            following = text[spans[index+1][0]:spans[index+1][1]].strip() if index+1 < len(spans) else ''
            kind = K.CORRECTION if following and (re.match(r'^[.,]?[0-9]', following)
                or _DIALOGUE_CORRECTION.match(following)) else K.CANCELLATION
        elif head == 'хотя нет' or head in _LEXEMES or head in ('не надо', 'не нужно', 'не треба'):
            kind = K.CANCELLATION
        elif cancellation_words and _ACTION_REF.search(head) and not reason:
            kind = K.CANCELLATION
        elif _CORRECTION.match(head) or re.match(r'^а\s+[.,]?[0-9]', raw):
            kind = K.CORRECTION
        elif _CORRECTION_WORD.search(head) and (active or contextual):
            kind = K.UNKNOWN
        elif reason and _ACTION_REF.search(reason_head) and reason_tokens and _typos(reason_tokens[0]):
            kind = K.UNKNOWN
        elif reason:
            kind = K.COMMENT
        else:
            # Typo recognition is confined to an isolated final clause/head
            # with a current-action reference. Never scan product names or
            # arbitrary prose for similar words; never repair a two-edit token.
            referent_tail = bool(tokens) and all(token in _REFERENTS for token in tokens[1:])
            eligible = bool(tokens) and (active or contextual) and (
                (index == len(spans)-1 and (index > 0 or contextual) and referent_tail)
                or bool(_ACTION_REF.search(head)))
            matches = _typos(tokens[0]) if eligible else ()
            if matches:
                kind = K.CANCELLATION if len(matches) == 1 else K.CONTRADICTION
            elif eligible and (active or _ACTION_REF.search(head)) and len(tokens[0]) >= 5 and referent_tail and tokens[0] not in _REASONS:
                kind = K.UNKNOWN
            elif active and index > 0 and len(tokens) == 2 and tokens[0] == 'не' and not _DESCRIPTIVE_NEGATION.fullmatch(head):
                kind = K.UNKNOWN
            elif active and index > 0 and index == len(spans)-1 and len(tokens) == 1 and not re.search(r'[0-9]',head) and tokens[0] not in _REASONS:
                # An unrecognised isolated tail is not silently declared a
                # harmless comment (also covers two-edit cancellation damage).
                kind = K.UNKNOWN
            elif (active or contextual) and (index > 0 or contextual) and _ACTION_REF.search(head):
                kind = K.UNKNOWN
            else:
                kind = K.ARGUMENTS if index == 0 or re.search(r'[0-9]',head) else K.COMMENT
        clause = MutationClause(kind=kind, start=start, end=end)
        clauses.append(clause)
        if kind == K.CANCELLATION:
            cancellations.append(index)
        if kind in (K.CONTRADICTION, K.UNKNOWN):
            contradictions.append(index)
        if kind == K.ACTION:
            actions.append(index)
    if contradictions:
        return ClauseAnalysis(tuple(clauses), D.AMBIGUOUS)
    if cancellations:
        last_cancel = cancellations[-1]
        later_actions = [i for i in actions if i > last_cancel]
        if len(later_actions) == 1:
            target = later_actions[0]
            raw = text[clauses[target].start:clauses[target].end]
            head = _CONNECTORS.sub('',raw.strip()).strip()
            return ClauseAnalysis(tuple(clauses), restart_at=clauses[target].start + raw.index(head))
        if any(c.kind == K.CORRECTION for c in clauses[last_cancel+1:]):
            return ClauseAnalysis(tuple(clauses), D.AMBIGUOUS)
        return ClauseAnalysis(tuple(clauses), D.CANCEL)
    # Two commands on a comma-separated line cannot disappear as a comment.
    # Explicit newline batch rows remain owned by the existing intake parser.
    if len(actions) > 1 and any('\n' not in text[clauses[a].end:clauses[b].start]
        for a,b in zip(actions,actions[1:])):
        return ClauseAnalysis(tuple(clauses), D.AMBIGUOUS)
    return ClauseAnalysis(tuple(clauses))
