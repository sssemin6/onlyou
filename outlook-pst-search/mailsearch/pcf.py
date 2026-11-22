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


WORD_SUFFIXES = {".doc", ".docx"}
PCF_CELL = re.compile(
    r"^PCF (T\d+R\d+) · No\. ([A-Za-z0-9._/-]+) · (T\d+R(\d+)C\d+) · (OWNER|YARD)$"
)
NUMBER = re.compile(r"(?:no\.?\s*)?([0-9][A-Za-z0-9._/-]{0,29})", re.I)
HEADERS = {
    "number": {"no", "number", "commentno", "commentnumber", "itemno", "번호", "코멘트번호", "의견번호"},
    "owner": {"owner", "ownerscomment", "ownercomment", "ownercomments", "shipowner", "선주", "선주의견"},
    "yard": {"yard", "yardreply", "yardresponse", "yardanswer", "builder", "조선소", "조선소회신"},
}


def _name(element):
    return element.tag.rsplit("}", 1)[-1]


def _child(element, name):
    return next((child for child in element if _name(child) == name), None) if element is not None else None


def _val(element, name="val"):
    if element is not None:
        return next((value for key, value in element.attrib.items() if key.rsplit("}", 1)[-1] == name), None)
    return None


def _cell_text(cell):
    paragraphs = []
    for paragraph in (child for child in cell if _name(child) == "p"):
        parts = []
        for element in paragraph.iter():
            tag = _name(element)
            if tag == "t":
                parts.append(element.text or "")
            elif tag in {"br", "cr", "tab"}:
                parts.append("\t" if tag == "tab" else "\n")
        paragraphs.append("".join(parts))
    return "\n".join(paragraphs)


def _word_grid(row):
    offset = _val(_child(_child(row, "trPr"), "gridBefore"))
    column = int(offset or 0)
    if not 0 <= column <= 64:
        raise ValueError("pcf_grid")
    for cell in (child for child in row if _name(child) == "tc"):
        props = _child(cell, "tcPr")
        span = int(_val(_child(props, "gridSpan")) or 1)
        if span < 1 or column + span > 64:
            raise ValueError("pcf_grid")
        yield column, span, _cell_text(cell), cell
        column += span


def _header(cells):
    fields = {}
    for start, span, text, _ in cells:
        name = re.sub(r"[^a-z0-9가-힣]", "", unicodedata.normalize("NFKC", text).casefold())
        matched = next((key for key, labels in HEADERS.items() if name in labels), None)
        if matched:
            if matched in fields:
                return None
            fields[matched] = range(start, start + span)
    if set(fields) == set(HEADERS) and len(fields["number"]) == 1:
        return fields
    return None


def word_comments(root, check):
    """Read supported top-level PCF tables, with unresolved rows reported separately."""
    body = _child(root, "body")
    comments, warnings = [], []
    for table_no, table in enumerate((e for e in (body if body is not None else root)
                                     if _name(e) == "tbl"), 1):
        fields, active = None, None
        for row_no, row in enumerate((e for e in table if _name(e) == "tr"), 1):
            check()
            cells = list(_word_grid(row))
            header = _header(cells)
            if header:
                if fields != header:
                    active = None
                fields = header
                continue
            if not fields:
                continue
            if _child(_child(row, "trPr"), "tblHeader") is not None:
                warnings.append(f"T{table_no}R{row_no} · 머리글 확인 필요")
                active, fields = None, None
                continue
            cols = {column: (span, text, cell) for column, span, text, cell in cells}
            number_column = fields["number"].start
            number_span, value, number_cell = cols.get(number_column, (0, "", None))
            merge = _child(_child(number_cell, "tcPr"), "vMerge")
            start = NUMBER.fullmatch(value.strip()) if number_span == 1 else None
            continuation = number_span == 1 and merge is not None and (_val(merge) or "continue") == "continue"
            if start and not continuation:
                active = {"table": table_no, "start_row": row_no, "number": start[1],
                          "number_text": value, "rows": [], "warnings": []}
                comments.append(active)
            elif not (continuation and not value.strip() and active and active["merged"]):
                active = None
            if active is None:
                warnings.append(f"T{table_no}R{row_no} · COMMENT 연결 확인 필요")
                continue
            if start:
                active["merged"] = merge is not None and _val(merge) == "restart"
            record = {"row": row_no}
            for side in ("owner", "yard"):
                bounds = fields[side]
                entries = [(column, *cols[column]) for column in bounds if column in cols]
                # Accept one text cell or a label followed by a text cell.
                if (not entries or len(entries) > 2 or entries[0][0] != bounds.start
                        or sum(entry[1] for entry in entries) != len(bounds)
                        or any(column + span > bounds.stop for column, span, _, _ in entries)):
                    record[side] = None
                    active["warnings"].append(f"R{row_no} · {side.upper()} 열 확인 필요")
                    continue
                column, _, text, cell = entries[-1]
                label = entries[0][2] if len(entries) == 2 else ""
                unsupported = {"tbl", "drawing", "pict", "object", "ins", "del", "moveFrom", "moveTo", "altChunk",
                               "vMerge", "hMerge", "sdt", "txbxContent"}  # text requiring separate interpretation
                if any(_name(e) in unsupported for _, _, _, item in entries for e in item.iter()):
                    active["warnings"].append(f"R{row_no} · {side.upper()} 셀의 추가 내용 확인 필요")
                    record[side] = None
                    continue
                record[side] = {"label": label, "text": text, "cell": f"T{table_no}R{row_no}C{column + 1}"}
            owner, yard = record["owner"], record["yard"]
            if owner and yard and owner["label"] and yard["label"] and owner["label"].strip() != yard["label"].strip():
                active["warnings"].append(f"R{row_no} · Owner/YARD 라벨 불일치")
            active["rows"].append(record)
    for comment in comments:
        comment.pop("merged")
    return comments, warnings


def case_key(comment):
    return f"T{comment['table']}R{comment['start_row']}"


def cell_location(comment, side, cell):
    return f"PCF {case_key(comment)} · No. {comment['number']} · {cell['cell']} · {side.upper()}"


def word_metadata(text):
    """Only explicitly labelled, unambiguous document metadata."""
    metadata = {}
    for field, label in {"document_no": r"(?:PCF|document)\s*(?:no\.?|number)|문서번호",
                         "revision": r"rev(?:ision)?\.?|개정번호",
                         "hull": PCF_FIELDS["hull"]}.items():
        values = set(re.findall(rf"^\s*(?:{label})\s*[:：=]\s*([^\n]{{1,80}})\s*$", text, re.I | re.M))
        metadata[field] = next(iter(values)).strip() if len(values) == 1 else None
    return metadata
