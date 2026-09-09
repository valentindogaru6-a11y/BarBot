"""Conservative normalization shared by warehouse review and writes."""

import re
import unicodedata

_WHITESPACE_RE = re.compile(r"\s+")
_EXTERNAL_PUNCTUATION = " \t\r\n.,;:!?\"'«»()[]{}-–—"


def normalize_warehouse_name(value: str) -> str:
    """Normalize comparison text without erasing meaningful inner content."""

    normalized = unicodedata.normalize("NFKC", value).casefold().replace("ё", "е")
    normalized = _WHITESPACE_RE.sub(" ", normalized).strip()
    return normalized.strip(_EXTERNAL_PUNCTUATION).strip()
