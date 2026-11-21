"""Extract searchable text and preserve attachment locations."""
import io
import os
import re
import shutil
import subprocess
import tempfile
import unicodedata
import zipfile
from datetime import timezone
from email import policy
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree as ET

MAX_MAIL = 180 * 1024**2
MAX_ATTACHMENT = 25 * 1024**2
MAX_TEXT = 300_000
MAX_XML_TOTAL = 48 * 1024**2
MAX_LOCATIONS = 800
CREATE_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)
DOCUMENT_SUFFIXES = {".txt", ".pdf", ".docx", ".xlsx", ".doc", ".xls"}
LEGACY_SUFFIXES = {".doc": ".docx", ".xls": ".xlsx"}
OCR_PAGE_MIN_TEXT = 30   # pdftotext pages shorter than this are scan candidates
OCR_MIN_RESULT = 20      # shorter OCR output is dropped as low confidence
OCR_PAGE_TIMEOUT = 30    # seconds per page for rendering and reading


class MailHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.text = []
        self.hidden = []

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "head", "template"}:
            self.hidden.append(tag)
        if not self.hidden and tag in {"br", "p", "div", "tr", "li", "blockquote"}:
            self.text.append("\n")
        if not self.hidden and tag in {"td", "th"}:
            self.text.append(" ")

    def handle_endtag(self, tag):
        if self.hidden and tag == self.hidden[-1]:
            self.hidden.pop()
        elif not self.hidden and tag in {"p", "div", "tr", "li", "blockquote"}:
            self.text.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.text.append(data)


def clean(text):
    return unicodedata.normalize("NFC", text).replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n").strip()


def decode(data):
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    for encoding in ("utf-8-sig", "cp949"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def parse_mail(path, folder):
    if path.stat().st_size > MAX_MAIL:
        raise ValueError("mail_size")
    msg = BytesParser(policy=policy.default).parsebytes(path.read_bytes())
    if not any(msg.get(header) for header in ("Subject", "From", "Date", "Message-ID", "To")):
        raise ValueError("mail_headers")
    date = ""
    try:
        sent = parsedate_to_datetime(str(msg.get("Date", "")))
        date = sent.replace(tzinfo=sent.tzinfo or timezone.utc).astimezone(timezone.utc).isoformat(timespec="seconds")
    except (ValueError, TypeError, OverflowError):
        pass
    body = msg.get_body(preferencelist=("plain", "html"))
    body_text = ""
    if body is not None:
        try:
            body_text = body.get_content(errors="replace")
        except LookupError:
            body_text = decode(body.get_payload(decode=True) or b"")
        if body.get_content_subtype() == "html":
            parser = MailHTML()
            parser.feed(body_text)
            parser.close()
            body_text = "".join(parser.text)
    attachments = []
    for part in msg.walk():
        name = part.get_filename()
        if name or part.get_content_disposition() == "attachment":
            attachments.append((clean(str(name or "이름 없는 첨부"))[:512], part))
    return {
        "folder": folder, "subject": clean(str(msg.get("Subject", ""))), "sender": clean(str(msg.get("From", ""))),
        "recipient": "\n".join(clean(str(msg.get(h, ""))) for h in ("To", "Cc", "Bcc")).strip(),
        "sent": date, "message_id": str(msg.get("Message-ID", "")).strip().lower(),
        "reply_ids": (str(msg.get("References", "")) + " " + str(msg.get("In-Reply-To", ""))).lower().strip(),
        "body": clean(body_text), "attachments": attachments,
    }


def local_name(tag):
    return tag.rsplit("}", 1)[-1]


def xml_member(zipped, name):
    if zipped.getinfo(name).file_size > MAX_XML_TOTAL:
        raise ValueError("xml_size")
    data = zipped.read(name)
    # Office XML does not need DTDs or entity declarations.
    if re.search(rb"<!\s*(?:DOCTYPE|ENTITY)", data.replace(b"\0", b""), re.I):
        raise ValueError("xml_dtd")
    return ET.fromstring(data)


def office_text(data, suffix):
    with zipfile.ZipFile(io.BytesIO(data)) as zipped:
        members = zipped.infolist()
        if len(members) > 3000 or sum(x.file_size for x in members if x.filename.endswith((".xml", ".rels"))) > MAX_XML_TOTAL:
            raise ValueError("office_size")
        if suffix == ".docx":
            root = xml_member(zipped, "word/document.xml")
            lines = [
                "".join(t.text or "" for t in para.iter() if local_name(t.tag) == "t")
                for para in root.iter() if local_name(para.tag) == "p"
            ]
            yield "본문", "\n".join(lines)
            return
        book = xml_member(zipped, "xl/workbook.xml")
        rels = xml_member(zipped, "xl/_rels/workbook.xml.rels")
        paths = {r.get("Id"): r.get("Target", "") for r in rels if r.get("TargetMode") != "External"}
        strings = []
        if "xl/sharedStrings.xml" in zipped.namelist():
            strings = [
                "".join(t.text or "" for t in si.iter() if local_name(t.tag) == "t")
                for si in xml_member(zipped, "xl/sharedStrings.xml") if local_name(si.tag) == "si"
            ]
        for sheet in book.iter():
            if local_name(sheet.tag) != "sheet":
                continue
            ref = next((value for name, value in sheet.attrib.items() if local_name(name) == "id"), "")
            target = paths.get(ref, "")
            member = target.lstrip("/") if target.startswith("/") else f"xl/{target}"
            if not target or ".." in PurePosixPath(member).parts:
                continue
            for row in xml_member(zipped, member).iter():
                if local_name(row.tag) != "row":
                    continue
                cells = []
                for cell in row:
                    if local_name(cell.tag) != "c":
                        continue
                    kind = cell.get("t")
                    value = next((el.text or "" for el in cell if local_name(el.tag) == "v"), "")
                    if kind == "s":
                        value = strings[int(value)] if len(value) <= 9 and value.isascii() and value.isdecimal() and int(value) < len(strings) else ""
                    elif kind == "inlineStr":
                        value = "".join(el.text or "" for el in cell.iter() if local_name(el.tag) == "t")
                    if value:
                        cells.append((cell.get("r", "?"), value))
                if cells:
                    yield f"{sheet.get('name', '시트')}!{cells[0][0]}~{cells[-1][0]}", " | ".join(f"{ref} {value}" for ref, value in cells)


def office_binary():
    """Locate a read-only LibreOffice converter for legacy DOC/XLS files."""
    candidates = [shutil.which("soffice"), shutil.which("soffice.exe")]
    for root in (os.environ.get("PROGRAMFILES"), "C:\\Program Files", "C:\\Program Files (x86)"):
        if root:
            candidates.append(Path(root) / "LibreOffice" / "program" / "soffice.exe")
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(candidate)
    return None


def convert_legacy(source, suffix, temporary):
    """Convert a legacy Office file to its OOXML form without running the original."""
    binary = office_binary()
    if not binary:
        return None, "변환기 미설치(구형 Office)"
    with tempfile.TemporaryDirectory(dir=temporary, prefix="convert-") as directory:
        target = Path(directory) / ("input" + suffix)
        shutil.copyfile(source, target)
        try:
            subprocess.run(
                [binary, "--headless", "--convert-to", LEGACY_SUFFIXES[suffix], "--outdir", directory, str(target)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=90, check=True, creationflags=CREATE_FLAGS,
            )
        except (OSError, subprocess.SubprocessError):
            return None, "추출 실패 또는 암호화"
        converted = Path(directory) / ("input" + LEGACY_SUFFIXES[suffix])
        if not converted.is_file():
            return None, "추출 실패 또는 암호화"
        return converted.read_bytes(), None


def tesseract_binary():
    """Locate the tesseract OCR engine, honoring MAILSEARCH_TESSERACT first."""
    candidates = [os.environ.get("MAILSEARCH_TESSERACT"), shutil.which("tesseract"), shutil.which("tesseract.exe")]
    for root in (os.environ.get("PROGRAMFILES"), "C:\\Program Files", "C:\\Program Files (x86)"):
        if root:
            candidates.append(Path(root) / "Tesseract-OCR" / "tesseract.exe")
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(candidate)
    return None


def pdftoppm_binary():
    """Locate a page renderer for OCR; Git for Windows ships a portable one."""
    candidates = [shutil.which("pdftoppm"), shutil.which("pdftoppm.exe")]
    for root in (os.environ.get("PROGRAMFILES"), "C:\\Program Files", "C:\\Program Files (x86)"):
        if root:
            candidates.append(Path(root) / "Git" / "mingw64" / "bin" / "pdftoppm.exe")
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(candidate)
    return None


def ocr_page(input_pdf, page, renderer, reader, check):
    """Render one PDF page and read it: kor+eng, then eng, then the default model."""
    stem = input_pdf.parent / f"ocr-{page}"
    try:
        subprocess.run(
            [renderer, "-f", str(page), "-l", str(page), "-singlefile", "-r", "200", "-png",
             str(input_pdf), str(stem)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=OCR_PAGE_TIMEOUT, check=True, creationflags=CREATE_FLAGS,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    image = stem.with_suffix(".png")
    if not image.is_file():
        return None
    for language in ("kor+eng", "eng", ""):
        check()
        command = [reader] + (["-l", language] if language else []) + [str(image), "stdout"]
        try:
            done = subprocess.run(
                command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                timeout=OCR_PAGE_TIMEOUT, check=True, creationflags=CREATE_FLAGS,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        return clean(done.stdout.decode("utf-8", errors="replace"))
    return None


def pdf_pages(input_pdf, pages, check):
    """Locate page text; near-empty pages fall back to OCR when tools exist."""
    locations = [(f"{index}페이지", value) for index, value in enumerate(pages, 1)]
    scanned = [index for index, value in enumerate(pages, 1) if len(clean(value)) < OCR_PAGE_MIN_TEXT]
    if not scanned:
        return locations, None
    renderer, reader = pdftoppm_binary(), tesseract_binary()
    completed = 0
    if renderer and reader:
        for page in scanned:
            check()
            text = ocr_page(input_pdf, page, renderer, reader, check)
            if text is not None and len(text) >= OCR_MIN_RESULT:
                locations[page - 1] = (f"{page}페이지(OCR)", text)
                completed += 1
    status = f"일부 색인 · OCR {completed}/{len(scanned)}쪽" if completed < len(scanned) else None
    return locations, status


def extract_bytes(content, suffix, temporary, pdf_command, *, check=lambda: None):
    """(status, [(location, text)]) for document bytes; never raises."""
    if suffix not in {".txt", ".pdf", ".docx", ".xlsx"}:
        return "미지원 형식", []
    if not content:
        return "텍스트 없음", []
    if len(content) > MAX_ATTACHMENT:
        return "용량 초과", []
    try:
        ocr_status = None
        if suffix == ".txt":
            locations = [("본문", decode(content))]
        elif suffix == ".pdf":
            binary = shutil.which(pdf_command)
            if not binary:
                return "PDF 추출기 미설치", []
            with tempfile.TemporaryDirectory(dir=temporary, prefix="pdf-") as directory:
                input_pdf = Path(directory) / "attachment.pdf"
                output_txt = Path(directory) / "attachment.txt"
                input_pdf.write_bytes(content)
                subprocess.run(
                    [binary, "-enc", "UTF-8", "-l", "800", str(input_pdf), str(output_txt)],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=45, check=True, creationflags=CREATE_FLAGS,
                )
                with output_txt.open("rb") as output:
                    buffer = output.read(MAX_TEXT * 4 + 1)
                pages = buffer[:MAX_TEXT * 4].decode("utf-8", errors="replace").split("\f")
                if pages and not pages[-1]:
                    pages.pop()
                locations, ocr_status = pdf_pages(input_pdf, pages, check)
        else:
            locations = office_text(content, suffix)
        result = []
        remaining = MAX_TEXT
        limited = suffix == ".pdf" and (len(buffer) > MAX_TEXT * 4 or len(pages) >= 800)
        for location, raw_text in locations:
            text = clean(raw_text)
            if not text:
                continue
            if not remaining or len(result) >= MAX_LOCATIONS:
                limited = True
                break
            limited |= len(text) > remaining
            result.append((location, text[:remaining]))
            remaining -= len(text[:remaining])
        if not result:
            return ocr_status or "텍스트 없음", result
        return ("일부 색인" if limited else ocr_status or "완료"), result
    except (ValueError, OSError, KeyError, IndexError, RuntimeError, ET.ParseError,
            zipfile.BadZipFile, subprocess.SubprocessError):
        return "추출 실패 또는 암호화", []


def extract_document(path, temporary, pdf_command, *, check=lambda: None):
    """(status, [(location, text)]) for a standalone document file."""
    suffix = path.suffix.lower()
    if suffix not in DOCUMENT_SUFFIXES:
        return "미지원 형식", []
    if suffix in LEGACY_SUFFIXES:
        content, failure = convert_legacy(path, suffix, temporary)
        if content is None:
            return failure, []
        return extract_bytes(content, LEGACY_SUFFIXES[suffix], temporary, pdf_command, check=check)
    try:
        if path.stat().st_size > MAX_ATTACHMENT:
            return "용량 초과", []
        content = path.read_bytes()
    except OSError:
        return "추출 실패 또는 암호화", []
    return extract_bytes(content, suffix, temporary, pdf_command, check=check)


def extract_attachment(name, part, temporary, pdf_command, *, check=lambda: None):
    return extract_bytes(part.get_payload(decode=True), Path(name).suffix.lower(), temporary, pdf_command, check=check)
