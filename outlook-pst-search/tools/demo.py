"""Generate synthetic EML messages for an offline demo."""
import argparse
from email.message import EmailMessage
from pathlib import Path

MESSAGES = [
    ("initial", "03", "한빛 A-17 출하 일정", "한빛 A-17은 4월 18일 납품으로 진행하겠습니다.", None),
    ("request", "09", "Re: 한빛 A-17 출하 일정", "자재 입고 지연으로 4월 25일로 납품 일정 변경 요청드립니다.", "initial"),
    ("approved", "10", "Re: 한빛 A-17 출하 일정", "요청 주신 4월 25일 납품으로 최종 승인합니다.", "request"),
    ("other", "11", "한빛 B-19 견적", "견적은 첨부 파일을 참고해 주세요.", None),
]


def generate(folder):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    for name, day, subject, body, parent in MESSAGES:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = "구매팀 <buy@example.invalid>"
        msg["To"] = "한빛 담당자 <hanbit@example.invalid>"
        msg["Date"] = f"{day} Apr 2026 09:00:00 +0900"
        msg["Message-ID"] = f"<{name}@example.invalid>"
        if parent:
            msg["In-Reply-To"] = f"<{parent}@example.invalid>"
            msg["References"] = "<initial@example.invalid>" + (f" <{parent}@example.invalid>" if parent != "initial" else "")
        msg.set_content(body)
        if name == "other":
            msg.add_attachment("키워드 반도체냉각판. B-19 공급가 320000원.\n".encode(), maintype="text", subtype="plain", filename="B-19_견적.txt")
            msg.add_attachment(b"example", maintype="application", subtype="octet-stream", filename="B-19_도면.hwp")
        (folder / f"{name}.eml").write_bytes(msg.as_bytes())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder")
    args = parser.parse_args()
    generate(args.folder)
    print(Path(args.folder).resolve())
