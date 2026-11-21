"""Identify labelled attributes and correspondence numbers in PCF documents."""
import re
import unicodedata
from pathlib import Path


PCF_FIELDS = {
    "class": r"선급|class(?:ification)?(?: society)?",
    "flag": r"기국|flag(?: state)?",
    "owner": r"선주|(?:ship[ -]?)?owner",
    "hull": r"호선(?: 번호)?|hull(?:\s+(?:no\.?|number))?",
    "drawing": r"도면명|drawing(?:\s+(?:name|title))?",
}
FIELD_PATTERNS = {
    key: re.compile(r"(?:^|[\n|;])\s*(?:" + label + r")\s*[:：=]\s*([^\n|;]+)", re.IGNORECASE)
    for key, label in PCF_FIELDS.items()
}
HULL_IN_FILENAME = re.compile(r"(?<![A-Z0-9])H\d+(?![A-Z0-9])", re.IGNORECASE)
COMMENT_NUMBER = re.compile(
    r"(?:comment\s*(?:no\.?|number|#)|(?:comment|의견)\s*번호)\s*[:：=]?\s*([A-Z0-9][A-Z0-9._/-]*)",
    re.IGNORECASE,
)
COMMENT_HEADER = re.compile(r"(?:^|[\n|])[^\S\n]*(" + COMMENT_NUMBER.pattern + r")", re.IGNORECASE)


def pcf_field_value(field, filename, text, expected):
    def clean(value):
        return " ".join(unicodedata.normalize("NFKC", value).casefold().split())

    values = FIELD_PATTERNS[field].findall(text)
    if expected is None:
        return bool(values)
    if values:
        return all(clean(value) == clean(expected) for value in values)
    if field == "hull":
        numbers = HULL_IN_FILENAME.findall(Path(filename).stem)
        return len(numbers) == 1 and clean(numbers[0]) == clean(expected)
    return False


def comment_number(text):
    numbers = {match.casefold() for match in COMMENT_NUMBER.findall(text)}
    return next(iter(numbers)) if len(numbers) == 1 else None
