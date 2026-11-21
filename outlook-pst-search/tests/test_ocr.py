"""OCR of scanned PDF pages exercised with fake poppler and tesseract tools."""
import os
import tempfile
import unittest
from pathlib import Path

from mailsearch.extract import extract_bytes

PAGE_ONE = "Braket inspection report with enough words to pass the plain text threshold."
PAGE_THREE = "Final approval notes are stored on this fully text-based page."
OCR_SAMPLE = "스캔 페이지에서 읽어낸 한글 텍스트입니다. 진동 시험 결과 12쪽."

FAKE_PDFTOTEXT = (
    "@echo off\r\n"
    "rem fake pdftotext: copy prepared pages to the output path (last argument)\r\n"
    "set \"out=\"\r\n"
    "for %%A in (%*) do set \"out=%%~A\"\r\n"
    "copy /y \"%~dp0pages.txt\" \"%out%\" >nul\r\n"
)

FAKE_PDFTOPPM = (
    "@echo off\r\n"
    "rem fake pdftoppm: create the expected singlefile png (last argument is the stem)\r\n"
    "set \"stem=\"\r\n"
    "for %%A in (%*) do set \"stem=%%~A\"\r\n"
    "type nul > \"%stem%.png\"\r\n"
)

FAKE_TESSERACT = (
    "@echo off\r\n"
    "rem fake tesseract: print sample OCR text to stdout\r\n"
    "type \"%~dp0ocr_sample.txt\"\r\n"
)

FAKE_TESSERACT_NO_KOR = (
    "@echo off\r\n"
    "rem fake tesseract without kor traineddata: fail kor+eng, succeed otherwise\r\n"
    "if \"%2\" == \"kor+eng\" exit /b 1\r\n"
    "type \"%~dp0ocr_sample.txt\"\r\n"
)


class ScanPdfOcr(unittest.TestCase):
    def setUp(self):
        workspace = tempfile.TemporaryDirectory()
        self.addCleanup(workspace.cleanup)
        self.root = Path(workspace.name)
        self.old_path = os.environ["Path"]
        self.addCleanup(lambda: os.environ.__setitem__("Path", self.old_path))
        self.addCleanup(os.environ.pop, "MAILSEARCH_TESSERACT", None)

    def make_tools(self, tesseract, sample=OCR_SAMPLE):
        tools = self.root / "tools"
        tools.mkdir()
        (tools / "pages.txt").write_bytes(f"{PAGE_ONE}\f\f{PAGE_THREE}".encode("utf-8"))
        (tools / "ocr_sample.txt").write_bytes(sample.encode("utf-8"))
        for name, body in (("pdftotext.cmd", FAKE_PDFTOTEXT), ("pdftoppm.cmd", FAKE_PDFTOPPM),
                           ("tesseract.cmd", tesseract)):
            (tools / name).write_text(body, encoding="ascii", newline="")
        return tools

    def use_ocr_tools(self, tools):
        os.environ["MAILSEARCH_TESSERACT"] = str(tools / "tesseract.cmd")
        os.environ["Path"] = str(tools) + os.pathsep + self.old_path

    def test_scanned_page_gets_ocr_text(self):
        tools = self.make_tools(FAKE_TESSERACT)
        self.use_ocr_tools(tools)
        status, locations = extract_bytes(b"%PDF-fake", ".pdf", self.root, "pdftotext")
        self.assertEqual(status, "완료")
        self.assertEqual([name for name, _ in locations], ["1페이지", "2페이지(OCR)", "3페이지"])
        self.assertIn(OCR_SAMPLE, dict(locations)["2페이지(OCR)"])

    def test_tesseract_falls_back_to_english(self):
        tools = self.make_tools(FAKE_TESSERACT_NO_KOR)
        self.use_ocr_tools(tools)
        status, locations = extract_bytes(b"%PDF-fake", ".pdf", self.root, "pdftotext")
        self.assertEqual(status, "완료")
        self.assertIn("2페이지(OCR)", dict(locations))
        self.assertIn(OCR_SAMPLE, dict(locations)["2페이지(OCR)"])

    def test_low_confidence_ocr_page_is_dropped(self):
        tools = self.make_tools(FAKE_TESSERACT, sample="너무 짧은 인식 결과")
        self.use_ocr_tools(tools)
        status, locations = extract_bytes(b"%PDF-fake", ".pdf", self.root, "pdftotext")
        self.assertEqual(status, "완료")
        self.assertEqual([name for name, _ in locations], ["1페이지", "3페이지"])

    def test_without_ocr_tools_text_pages_only(self):
        plain = self.root / "plain"
        plain.mkdir()
        (plain / "pages.txt").write_bytes(f"{PAGE_ONE}\f\f{PAGE_THREE}".encode("utf-8"))
        (plain / "pdftotext.cmd").write_text(FAKE_PDFTOTEXT, encoding="ascii", newline="")
        system32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
        os.environ["Path"] = str(plain) + os.pathsep + str(system32)
        status, locations = extract_bytes(b"%PDF-fake", ".pdf", plain, "pdftotext")
        self.assertEqual(status, "완료")
        self.assertEqual([name for name, _ in locations], ["1페이지", "3페이지"])


if __name__ == "__main__":
    unittest.main()
