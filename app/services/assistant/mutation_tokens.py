"""Pure bounded Unicode inspection and positional tokens, without text repair.

Detection-only text must NEVER be used as an executable command. Normalized
values are excluded from repr/dumps; diagnostics carry only closed metadata.
"""

from dataclasses import dataclass, field
import re
import unicodedata

from app.models.assistant_mutation import (
    MutationToken, MutationTokenKind as T, UnicodeFlag as U, UnicodeInspection,
)

CANCEL_WORDS = frozenset(('отмена','отмени','отменить','отменяй','отбой','стоп',
    'скасуй','скасувати','скасування','видмина','передумал','передумала','передумав'))
CORRECTION_WORDS = frozenset(('нет','ни','точнее','точнише','исправь','замени','виправ'))
REASON_WORDS = frozenset(('причина','комментарий','потому','так','бо','тому'))
_SERVICE_WORDS = CANCEL_WORDS | CORRECTION_WORDS | frozenset((
    'спиши','списать','списуй','списати','списывай','списывать','списание','списання',
    'перемести','переместить','перемисти','перемищення','перемещение','перенеси','перенести',
    'отправь','делай','делать','не','забудь','надо','нужно','треба'))
_BIDI = frozenset(range(0x202A,0x202F)) | frozenset(range(0x2066,0x206A)) | {0x061C,0x200E,0x200F}
_JOINS = {0x200B,0x200C,0x200D,0x2060,0xFEFF,0xAD}
# Unicode default-ignorable exceptions not already covered by Cf/marks. Some
# fillers are letters or unassigned codepoints, so category checks alone miss
# them. Closed ranges, bounded input; never use this set to repair commands.
_IGNORABLE = {0x115F,0x1160,0x17B4,0x17B5,0x180F,0x2065,0x3164,0xFFA0}
_LOOKALIKE = str.maketrans({'a':'а','c':'с','e':'е','o':'о','p':'р','x':'х',
    'y':'у','k':'к','m':'м','t':'т','h':'н','b':'в','i':'и'})
_WORD = re.compile(r'[^\W\d_]+',re.UNICODE)
_ATOM = re.compile(r'[+−–—-]?(?:[0-9]+(?:[.,][0-9]*)*|[.,][0-9]+)|'
    r'[^\W\d_]+(?:[-\x27][^\W\d_]+)*|->|[^\w\s]',re.UNICODE)
_NUMBER = re.compile(r'[+−–—-]?(?:[0-9]+(?:[.,][0-9]*)*|[.,][0-9]+)$')


def normalize_command(value: str) -> str:
    return unicodedata.normalize('NFKC',value).casefold().replace('ё','е').replace('і','и').strip()


def inspect_unicode(value: str) -> UnicodeInspection:
    flags=set()
    positions=[]
    forbidden=False
    if len(value)>4000:
        return UnicodeInspection(normalized_text='',detection_text='',flags=(U.LENGTH,),forbidden=True)
    normalized=normalize_command(value)
    if len(normalized)>4000:
        return UnicodeInspection(normalized_text='',detection_text='',flags=(U.LENGTH,),forbidden=True)
    for index,char in enumerate(value):
        code=ord(char)
        category=unicodedata.category(char)
        bad=False
        if char.isspace():
            flags.add(U.SPACE if char in ' \t\n\r' else U.UNICODE_SPACE)
        if category=='Cf':
            flags.add(U.FORMAT);bad=True
        if category=='Cc':
            flags.add(U.CONTROL)
            if char not in '\t\n\r':bad=True
        if code in _JOINS:
            flags.add(U.JOINER);bad=True
        if code in _BIDI:
            flags.add(U.BIDI);bad=True
        if code in _IGNORABLE or 0xFFF0<=code<=0xFFF8 or 0xE0000<=code<=0xE0FFF:
            flags.add(U.IGNORABLE);bad=True
        if 0xFE00<=code<=0xFE0F or 0xE0100<=code<=0xE01EF or 0x180B<=code<=0x180D:
            flags.add(U.VARIATION);bad=True
        if bad:
            forbidden=True
            if len(positions)<32:positions.append(index)
    # Canonically composed letters remain normal letters. Residual marks are
    # not silently stripped, even in product names or comments.
    if any(unicodedata.category(char).startswith('M') for char in normalized):
        flags.add(U.COMBINING);forbidden=True
    # The detection representation removes controls/marks ONLY for inspection.
    detection=''.join(char for char in normalized if unicodedata.category(char) not in ('Cf','Cc')
        and not unicodedata.category(char).startswith('M'))
    for match in _WORD.finditer(normalized):
        word=match.group()
        scripts={script for char in word for script in ('LATIN','CYRILLIC') if script in unicodedata.name(char,'')}
        if len(scripts)>1:
            flags.add(U.MIXED_SCRIPT)
            if word.translate(_LOOKALIKE) in _SERVICE_WORDS:
                flags.add(U.HOMOGLYPH);forbidden=True
    # Spaces are never joined for execution. A separator splitting a service
    # lexeme requires clarification rather than manufacturing that lexeme.
    words=list(_WORD.finditer(normalized))
    max_keyword=max(map(len,_SERVICE_WORDS))
    for index,word in enumerate(words):
        joined=word.group()
        for tail in words[index+1:index+max_keyword]:
            if not normalized[word.end():tail.start()].isspace():
                break
            joined+=tail.group()
            if len(joined)>max_keyword:
                break
            if joined in _SERVICE_WORDS:
                flags.add(U.BOUNDARY);forbidden=True
            word=tail
    return UnicodeInspection(normalized_text=normalized,detection_text=detection,
        flags=tuple(sorted(flags,key=str)),positions=tuple(positions),forbidden=forbidden)


@dataclass(frozen=True)
class ClauseTokens:
    tokens: tuple[MutationToken,...]
    service_text: str = field(repr=False)
    ambiguous_entity: bool = False


def tokenize_clause(text: str, *, start: int, end: int, clause_index: int,
                    prefix_end: int | None, units: frozenset[str],
                    exact_entities: frozenset[str] = frozenset(),
                    argument_context=False, reason=False) -> ClauseTokens:
    """Resolve slots before considering a keyword's service role.

    Quantity atoms and their offsets are retained, including malformed numeric
    segments. Actual quantity acceptance still belongs to the Decimal parser.
    """
    output=[]
    for match in _ATOM.finditer(text,start,end):
        value=match.group()
        if _NUMBER.fullmatch(value):kind=T.QUANTITY
        elif value in units:kind=T.UNIT
        elif value in CANCEL_WORDS or value in ('отмену','отмены'):kind=T.CANCEL
        elif value in CORRECTION_WORDS:kind=T.CORRECTION
        elif value in REASON_WORDS:kind=T.REASON
        elif _WORD.fullmatch(value) or re.fullmatch(r'[^\W\d_]+-[^\W\d_]+',value):kind=T.UNKNOWN
        else:kind=T.BOUNDARY
        output.append(MutationToken(normalized_value=value,kind=kind,clause_index=clause_index,
            start=match.start(),end=match.end(),suspicious_unicode=False))
        if len(output)>256:
            return ClauseTokens((), '',True)
    ambiguous=False
    if prefix_end is None and argument_context and not reason and any(t.kind==T.QUANTITY for t in output):
        # Implicit batch rows retain their product/amount slots. A direct
        # imperative cancel remains service syntax, unlike a product noun Стоп.
        first_quantity=next(t.start for t in output if t.kind==T.QUANTITY)
        for i,t in enumerate(output):
            if t.kind==T.UNKNOWN or (t.kind==T.CANCEL and t.start<first_quantity
                and t.normalized_value not in ('отмени','отменить','отменяй','скасуй','скасувати')):
                output[i]=t.model_copy(update={'kind':T.ENTITY})
    if prefix_end is not None:
        number=next((token.start for token in output if token.kind==T.QUANTITY and token.start>=prefix_end),None)
        for i,token in enumerate(output):
            if token.start<prefix_end and token.kind!=T.BOUNDARY:
                output[i]=token.model_copy(update={'kind':T.ACTION})
            elif token.start>=prefix_end and token.kind!=T.BOUNDARY and (
                number is None or token.start<number):
                # An explicit action's pre-quantity slot is an entity, not a
                # cancel command, even if the exact product is named Стоп.
                if number is not None or token.kind!=T.UNIT:
                    output[i]=token.model_copy(update={'kind':T.ENTITY})
        if number is not None:
            body_tokens=[t for t in output if t.start>=prefix_end]
            before=[t for t in body_tokens if t.kind==T.ENTITY and t.start<number]
            tail=[t for t in body_tokens if t.start>number and t.kind not in (T.UNIT,T.QUANTITY,T.BOUNDARY)]
            tail_text=' '.join(t.normalized_value for t in tail)
            if not before:
                for i,t in enumerate(output):
                    if t.start>number and t.kind==T.UNKNOWN:
                        output[i]=t.model_copy(update={'kind':T.ENTITY})
            if not before and any(t.kind==T.CANCEL for t in tail):
                if tail_text in exact_entities:
                    for i,t in enumerate(output):
                        if t in tail:output[i]=t.model_copy(update={'kind':T.ENTITY})
                else:
                    ambiguous=True
    service=list(text[start:end])
    for token in output:
        if token.kind in (T.ACTION,T.ENTITY,T.QUANTITY,T.UNIT):
            service[token.start-start:token.end-start]=' '*(token.end-token.start)
    return ClauseTokens(tuple(output),''.join(service),ambiguous)
