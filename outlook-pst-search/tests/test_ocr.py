"""Exercise PDF extraction and OCR without relying on tools installed on the host."""
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from mailsearch.extract import extract_bytes

PAGE_ONE = "Bracket inspection report with enough words to pass the plain text threshold."
PAGE_THREE = "Final approval notes are stored on this fully text-based page."
OCR_SAMPLE = "스캔 페이지에서 읽어낸 한글 텍스트입니다. 진동 시험 결과 12쪽."


class ScanPdfOcr(unittest.TestCase):
    def test_pypdf_fallback_preserves_page_numbers_and_ocr(self):
        class PdfError(Exception):
            pass

        reader = Mock(return_value=SimpleNamespace(is_encrypted=False, pages=[
            Mock(**{"extract_text.return_value": text}) for text in (PAGE_ONE, "", PAGE_THREE)
        ]))
        module = SimpleNamespace(PdfReader=reader, errors=SimpleNamespace(PyPdfError=PdfError))
        with (tempfile.TemporaryDirectory() as temporary,
              patch("mailsearch.extract.optional_reader", return_value=module),
              patch("mailsearch.extract.pdftoppm_binary", return_value="pdftoppm"),
              patch("mailsearch.extract.tesseract_binary", return_value="tesseract"),
              patch("mailsearch.extract.ocr_page", return_value=OCR_SAMPLE) as ocr):
            result = extract_bytes(b"%PDF-fake", ".pdf", temporary, None)
            self.assertEqual(result, ("완료", [
                ("1페이지", PAGE_ONE), ("2페이지(OCR)", OCR_SAMPLE), ("3페이지", PAGE_THREE),
            ]))
            self.assertEqual(ocr.call_args.args[1], 2)
            self.assertEqual(reader.call_args.args[0].getvalue(), b"%PDF-fake")

            with (patch("mailsearch.extract.shutil.which", return_value="pdftotext"),
                  patch("mailsearch.extract.subprocess.run", side_effect=PermissionError),
                  patch("mailsearch.extract.MAX_TEXT", 90)):
                status, pages = extract_bytes(b"%PDF-fake", ".pdf", temporary, "pdftotext")
                self.assertEqual(status, "일부 색인")
                self.assertEqual(sum(len(text) for _, text in pages), 90)

            reader.return_value.is_encrypted = True
            self.assertEqual(extract_bytes(b"%PDF-fake", ".pdf", temporary, None), ("추출 실패 또는 암호화", []))
            reader.side_effect = PdfError
            self.assertEqual(extract_bytes(b"%PDF-fake", ".pdf", temporary, None), ("추출 실패 또는 암호화", []))
            with patch("mailsearch.extract.optional_reader", return_value=None):
                self.assertEqual(extract_bytes(b"%PDF-fake", ".pdf", temporary, None), ("PDF 추출기 미설치", []))

    def extract(self, pages, *, sample=OCR_SAMPLE, tools=True, english_only=False):
        def run(command, **options):
            program, *args = command
            if program == "pdftotext":
                Path(args[-1]).write_bytes(("\f".join(pages) + "\f").encode())
            elif program == "pdftoppm":
                Path(args[-1] + ".png").touch()
            elif english_only and "kor+eng" in args:
                raise subprocess.CalledProcessError(1, command)
            return subprocess.CompletedProcess(command, 0, stdout=sample.encode())

        with tempfile.TemporaryDirectory() as temporary:
            with (patch("mailsearch.extract.shutil.which", return_value="pdftotext"),
                  patch("mailsearch.extract.pdftoppm_binary", return_value="pdftoppm" if tools else None),
                  patch("mailsearch.extract.tesseract_binary", return_value="tesseract" if tools else None),
                  patch("mailsearch.extract.subprocess.run", side_effect=run)):
                return extract_bytes(b"%PDF-fake", ".pdf", Path(temporary), "pdftotext")

    def test_scanned_page_gets_ocr_text(self):
        status, locations = self.extract([PAGE_ONE, "", PAGE_THREE])
        self.assertEqual(status, "완료")
        self.assertEqual([name for name, _ in locations], ["1페이지", "2페이지(OCR)", "3페이지"])
        self.assertIn(OCR_SAMPLE, dict(locations)["2페이지(OCR)"])

    def test_tesseract_falls_back_to_english(self):
        status, locations = self.extract([PAGE_ONE, "", PAGE_THREE], english_only=True)
        self.assertEqual(status, "완료")
        self.assertIn(OCR_SAMPLE, dict(locations)["2페이지(OCR)"])

    def test_low_confidence_ocr_page_marks_partial(self):
        status, locations = self.extract([PAGE_ONE, "", PAGE_THREE], sample="짧은 인식 결과")
        self.assertEqual(status, "일부 색인 · OCR 0/1쪽")
        self.assertEqual([name for name, _ in locations], ["1페이지", "3페이지"])

    def test_without_ocr_tools_marks_partial(self):
        status, locations = self.extract([PAGE_ONE, "", PAGE_THREE], tools=False)
        self.assertEqual(status, "일부 색인 · OCR 0/1쪽")
        self.assertEqual([name for name, _ in locations], ["1페이지", "3페이지"])

    def test_ocr_continues_past_the_tenth_page(self):
        status, locations = self.extract([""] * 11)
        self.assertEqual(status, "완료")
        self.assertEqual(len(locations), 11)
        self.assertEqual(locations[-1], ("11페이지(OCR)", OCR_SAMPLE))


if __name__ == "__main__":
    unittest.main()
