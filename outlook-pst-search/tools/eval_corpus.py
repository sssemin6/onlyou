"""Build a synthetic Korean design-history corpus for retrieval evaluation (PRD 02 §7).

Each scenario is a subfolder mixing a test report (txt), approval or proposal
mails (eml), a production BOM (xlsx) and a revision draft (docx). SCENARIOS
lists the question and the text fragments that must surface as evidence.
"""
import io
import zipfile
from email.message import EmailMessage
from pathlib import Path


def _zip_bytes(documents):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zipped:
        for name, data in documents.items():
            zipped.writestr(name, data)
    return buffer.getvalue()


def _xlsx(sheet_name, cells):
    """cells: list of (ref, value) on one sheet."""
    row = "".join(
        f'<c r="{ref}" t="inlineStr"><is><t>{value}</t></is></c>' if isinstance(value, str)
        else f'<c r="{ref}"><v>{value}</v></c>'
        for ref, value in cells
    )
    return _zip_bytes({
        "xl/workbook.xml": f'<workbook xmlns:r="relationship"><sheets><sheet name="{sheet_name}" r:id="s1"/></sheets></workbook>',
        "xl/_rels/workbook.xml.rels": '<Relationships><Relationship Id="s1" Target="worksheets/sheet1.xml"/></Relationships>',
        "xl/worksheets/sheet1.xml": f"<worksheet><row>{row}</row></worksheet>",
    })


def _docx(text):
    return _zip_bytes({"word/document.xml": f"<document><p><r><t>{text}</t></r></p></document>"})


def _mail(subject, body, message_id, date, in_reply_to=None):
    message = EmailMessage()
    message["From"] = "review@example.invalid"
    message["To"] = "design@example.invalid"
    message["Subject"] = subject
    message["Date"] = date
    message["Message-ID"] = f"<{message_id}@example.invalid>"
    if in_reply_to:
        message["In-Reply-To"] = f"<{in_reply_to}@example.invalid>"
        message["References"] = f"<{in_reply_to}@example.invalid>"
    message.set_content(body)
    return message.as_bytes()


SCENARIOS = [
    {  # PRD 02 §5 acceptance scenario: prototype approved, production BOM differs.
        "id": "A17-prototype-only",
        "question": "A17 브래킷 두께가 왜 바뀌었고 양산에도 적용됐나?",
        "must_find": ["시제품만 4T 적용을 승인합니다", "3T", "전 모델 4T 적용 제안", "변형 발생 최대 0.18 mm"],
    },
    {
        "id": "A18-production-approved",
        "question": "A18 커버 두께 변경이 양산에 승인됐나?",
        "must_find": ["양산 전체 5T 적용을 승인합니다", "5T"],
    },
    {
        "id": "A19-revoked",
        "question": "A19 하우징 재질 변경은 최종적으로 어떻게 됐나?",
        "must_find": ["PC 재질로 변경을 승인합니다", "앞선 재질 변경 승인을 철회합니다"],
    },
    {
        "id": "A20-conflict",
        "question": "A20 브래킷 두께 기준은 무엇인가?",
        "must_find": ["두께는 3T로 확정한다", "기준서 기준 두께는 4T이다"],
    },
    {
        "id": "A21-unanswerable",
        "question": "A21 체결 볼트 토크 변경이 승인됐나?",
        "must_find": ["토크 측정값 12 N·m, 허용 15 N·m"],
    },
    {
        "id": "A22-conditional",
        "question": "A22 히트싱크 코팅 변경은 조건부 승인이었나?",
        "must_find": ["내염성 시험 통과를 조건으로 코팅 변경을 승인합니다", "내염성 시험을 통과했습니다"],
    },
    {
        "id": "A23-revision-chain",
        "question": "A23 샤프트 경계 치수는 Rev.B와 Rev.C에서 어떻게 다른가?",
        "must_find": ["경계 치수 12.0 mm로 정의한다", "경계 치수 12.5 mm로 개정한다", "Rev.C 반영을 완료했습니다"],
    },
    {
        "id": "A24-standard-section",
        "question": "A24 플랜지 공차 기준과 예외 조건은?",
        "must_find": ["§4.2 공차는 ±0.05 mm이다", "용접 조립품은 ±0.10 mm를 적용한다"],
    },
    {
        "id": "A25-table-values",
        "question": "A25 지지대 강성 시험의 측정값과 허용값은?",
        "must_find": ["0.42", "0.50"],
    },
    {
        "id": "A26-drawing-reflected",
        "question": "A26 브라켓 홀 위치 변경이 도면에 반영됐나?",
        "must_find": ["홀 위치 2 mm 이동을 승인합니다", "도면 Rev.B로 반영 완료했습니다"],
    },
]


def build(folder):
    """Write one subfolder per scenario and return the corpus root path."""
    folder = Path(folder)
    for scenario in SCENARIOS:
        item = scenario["id"].split("-")[0]
        target = folder / scenario["id"]
        target.mkdir(parents=True, exist_ok=True)
        if item == "A17":
            (target / "시험보고서.txt").write_text(
                "A17 브래킷 진동 시험 결과. 12쪽: 변형 발생 최대 0.18 mm, 허용 값 0.20 mm.", encoding="utf-8")
            (target / "승인메일.eml").write_bytes(_mail(
                "Re: A17 브래킷 두께 변경 승인", "A17 브래킷 두께 변경, 시제품만 4T 적용을 승인합니다.",
                "approve-a17", "03 May 2026 10:00:00 +0900"))
            (target / "양산BOM.xlsx").write_bytes(_xlsx("양산 BOM", [("F18", "3T")]))
            (target / "개정초안.docx").write_bytes(_docx("A17 전 모델 4T 적용 제안 초안입니다."))
        elif item == "A18":
            (target / "승인메일.eml").write_bytes(_mail(
                "Re: A18 커버 두께 변경 승인", "A18 커버 두께 변경, 양산 전체 5T 적용을 승인합니다.",
                "approve-a18", "07 Jun 2026 09:00:00 +0900"))
            (target / "양산BOM.xlsx").write_bytes(_xlsx("양산 BOM", [("C7", "5T")]))
        elif item == "A19":
            (target / "승인메일.eml").write_bytes(_mail(
                "Re: A19 하우징 재질 변경 승인", "A19 하우징 재질 ABS에서 PC 재질로 변경을 승인합니다.",
                "approve-a19", "01 Jul 2026 11:00:00 +0900"))
            (target / "철회메일.eml").write_bytes(_mail(
                "Re: A19 하우징 재질 변경 승인", "A19 관련, 앞선 재질 변경 승인을 철회합니다. 성능 재검토 후 재요청합니다.",
                "revoke-a19", "08 Jul 2026 16:00:00 +0900", in_reply_to="approve-a19"))
        elif item == "A20":
            (target / "확정메일.eml").write_bytes(_mail(
                "A20 브래킷 두께 확정", "A20 브래킷 두께는 3T로 확정한다는 회신을 드립니다.",
                "confirm-a20", "12 Aug 2026 13:00:00 +0900"))
            (target / "기준서.txt").write_text("A20 브래킷 기준서. 기준서 기준 두께는 4T이다.", encoding="utf-8")
        elif item == "A21":
            # No approval exists anywhere: the report is the only related evidence.
            (target / "시험보고서.txt").write_text(
                "A21 체결 볼트 토크 시험 결과. 토크 측정값 12 N·m, 허용 15 N·m.", encoding="utf-8")
        elif item == "A22":
            (target / "조건부승인.eml").write_bytes(_mail(
                "Re: A22 히트싱크 코팅 변경", "A22 히트싱크, 내염성 시험 통과를 조건으로 코팅 변경을 승인합니다.",
                "cond-a22", "19 Sep 2026 10:30:00 +0900"))
            (target / "조건충족.eml").write_bytes(_mail(
                "Re: A22 히트싱크 코팅 변경", "A22 코팅 샘플, 내염성 시험을 통과했습니다. 조건이 충족되었습니다.",
                "pass-a22", "26 Sep 2026 15:00:00 +0900", in_reply_to="cond-a22"))
        elif item == "A23":
            (target / "양산BOM RevB.xlsx").write_bytes(_xlsx("양산 BOM", [("D4", "12.0 mm")]))
            (target / "양산BOM RevC.xlsx").write_bytes(_xlsx("양산 BOM", [("D4", "12.5 mm")]))
            (target / "반영메일.eml").write_bytes(_mail(
                "A23 샤프트 경계 치수 개정", "A23 샤프트, 경계 치수 12.0 mm에서 경계 치수 12.5 mm로 개정한다. Rev.C 반영을 완료했습니다.",
                "rev-a23", "02 Oct 2026 09:30:00 +0900"))
        elif item == "A24":
            (target / "기준서.txt").write_text(
                "A24 플랜지 기준서. §4.2 공차는 ±0.05 mm이다. 단, 용접 조립품은 ±0.10 mm를 적용한다.", encoding="utf-8")
        elif item == "A25":
            (target / "시험보고서.txt").write_text(
                "A25 지지대 강성 시험 결과. 8쪽 표 참조.", encoding="utf-8")
            (target / "시험데이터.xlsx").write_bytes(
                _xlsx("강성 시험", [("B3", "측정값"), ("C3", "0.42"), ("B4", "허용값"), ("C4", "0.50")]))
        elif item == "A26":
            (target / "승인메일.eml").write_bytes(_mail(
                "Re: A26 브라켓 홀 위치 변경", "A26 브라켓, 홀 위치 2 mm 이동을 승인합니다.",
                "approve-a26", "14 Oct 2026 11:20:00 +0900"))
            (target / "도면반영.docx").write_bytes(_docx("A26 브라켓 도면 반영 통지. 도면 Rev.B로 반영 완료했습니다."))
    return folder


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder")
    args = parser.parse_args()
    root = build(args.folder)
    print(root.resolve())
