# 보관 메일 검색

여러 Outlook PST를 업무 PC에서 한 번 색인하고, 보관함 전체에서 품번·업체명·본문·첨부를 찾아보는 앱입니다. 로컬 AI를 연결하면 한국어로 질문할 수 있고, 답변의 인용을 눌러 원문을 확인할 수 있습니다.

[제품 요구사항 문서](docs/PRD.md)

## Windows에서 실행

1. GitHub에서 **Code → Download ZIP**으로 내려받고 압축을 풉니다. 사내망에서 GitHub에 접속할 수 없으면 내려받은 소스와 필요한 의존성을 사내 반입 방식으로 옮깁니다.
2. **setup-windows.cmd**를 한 번 실행합니다. 파이썬(없으면 winget으로 설치)과 의미 검색용 Ollama(선택)를 확인·설치하고, Ollama의 PC 시작 자동 실행을 꺼 줍니다. readpst와 bge-m3 임베딩 모델은 저장소에 이미 포함되어 있습니다.
3. **start.vbs**를 두 번 클릭하면 창 없이 실행되고 브라우저에서 앱이 열립니다. Ollama는 임베딩이 필요한 순간에만 백그라운드로 시작합니다. 콘솔 로그가 필요하면 `start-windows.cmd`를 직접 실행합니다.
4. 열린 브라우저에서 **보관함 추가**를 누른 뒤, Outlook을 닫고 로컬 디스크에 보관한 PST 경로를 등록합니다. 여러 파일은 한 번씩 등록하거나 PST를 모아 둔 폴더를 등록합니다. 폴더를 등록하면 그 안의 .eml 메일, 모든 PST, 문서 파일(PDF·DOCX·XLSX·TXT)을 함께 가져오며, 빈 PST나 읽을 수 없는 PST는 건너뛰고 누락으로 표시합니다. 문서 파일 하나만 따로 등록할 수도 있습니다. Outlook이 사용하는 데이터 파일 대신 닫혀 있는 PST 사본을 등록하면 파일 교체·갱신 시점을 제어하기 쉽습니다.
5. 브라우저에서 앱 탭을 모두 닫으면 앱도 종료됩니다(마지막 탭을 닫은 뒤 최대 10여 초 걸릴 수 있습니다). 콘솔로 실행한 경우 **Ctrl+C**로 종료합니다. 진행 중이던 색인은 다음 실행에서 **재개**할 수 있습니다.

인터넷이 막혀 setup-windows.cmd를 쓸 수 없거나 직접 준비하려면 FTS5가 포함된 **SQLite 3.42 이상**을 사용하는 **Python 3.12 이상**을 설치하세요. SQLite 버전은 `py -3 -c "import sqlite3; print(sqlite3.sqlite_version)"`으로 확인합니다. readpst는 저장소의 `vendor\readpst-win64\readpst.exe`를 그대로 쓰거나 [MSYS2 MinGW libpst](https://packages.msys2.org/packages/mingw-w64-x86_64-libpst)에서 직접 준비합니다. 텍스트 PDF 첨부를 색인하려면 `pdftotext`가 필요한데, Git for Windows가 설치된 PC에서는 그에 포함된 `pdftotext`를 자동으로 사용하므로 별도 설치가 없어도 됩니다. 없다면 [Poppler](https://poppler.freedesktop.org/)에서 준비합니다. PowerShell에서 앱 폴더로 이동하고 실행합니다. 도구가 PATH에 있으면 `--readpst`와 `--pdftotext`를 생략할 수 있습니다.

```powershell
.\start-windows.cmd --readpst "C:\msys64\mingw64\bin\readpst.exe" --pdftotext "C:\tools\poppler\bin\pdftotext.exe"
```

Linux에서도 SQLite 3.42 이상을 사용하는 Python이 필요합니다. `pst-utils`와 `poppler-utils`를 설치한 뒤 `python3 -m mailsearch`로 실행합니다. 파이썬 앱 자체는 표준 라이브러리만 사용합니다.

### 로컬 AI 켜기

임베딩 모델과 생성 모델은 모두 선택 사항입니다. 연결하지 않아도 일반 검색과 외부 API 답변을 쓸 수 있고, 임베딩 모델을 연결하면 질문의 의미로 메일을 찾는 의미 검색이 추가됩니다.

폐쇄망 반입 전에 사내에서 선택한 [Ollama](https://docs.ollama.com/) 설치 프로그램과 모델 파일을 준비해 둡니다. 생성 모델 예시는 `qwen3:4b`, 다국어 임베딩 모델 예시는 `bge-m3`입니다. 정확도와 지연은 장비에서 확인합니다. 네트워크가 허용되는 준비 환경에서 모델을 받아 모델 저장소를 반입하거나, 반입한 GGUF를 Ollama 로컬 모델로 등록할 수 있습니다. 예시 이름은 실제 설치된 로컬 모델과 일치해야 합니다.

PowerShell에서 로컬 추론 서버를 실행합니다. Ollama가 이미 실행 중이라면 아래 별도 포트로 시작합니다.

```powershell
$env:OLLAMA_NO_CLOUD = "1"
$env:OLLAMA_MAX_LOADED_MODELS = "1"
$env:OLLAMA_HOST = "127.0.0.1:11435"
ollama serve
```

다른 PowerShell에서 앱을 실행합니다.

```powershell
.\start-windows.cmd --model qwen3:4b --embedding-model bge-m3 --readpst "C:\msys64\mingw64\bin\readpst.exe"
```

설정 이전에 등록한 보관함은 **갱신**으로 의미 검색 색인을 생성합니다. 재색인 중 기존 버전으로 검색할 수 있습니다. 앱은 루프백 Ollama 포트에만 연결하고 해당 서버 목록에 있는 모델만 사용합니다. Ollama의 클라우드 기능과 외부 통신은 폐쇄망 방화벽 정책으로도 차단해 주세요.

### 임베딩 모델 포함과 Ollama 실행 방식

- bge-m3 모델(약 1.1GB)은 GitHub 파일 크기 제한 때문에 95MB 조각(`vendor\ollama-model\bge-m3\parts`)으로 저장소에 들어 있습니다. 앱을 처음 실행하면 데이터 폴더(`%LOCALAPPDATA%\OutlookMailSearch\ollama-models`)에서 조각을 합치고 SHA-256으로 검증합니다. 한 번 합치면 이후 실행에서는 건너뜁니다.
- setup-windows.cmd가 Ollama의 시작 프로그램 등록을 해제합니다. Ollama는 PC 부팅 때가 아니라 앱에서 임베딩이 필요한 순간(첫 검색 또는 AI 질문)에만 백그라운드로 시작하고, 앱이 끝나면 함께 종료됩니다. 첫 회는 모델을 읽는 데 수 초 걸립니다.
- 폐쇄망 등 수동 환경에서는 기존처럼 임의의 Ollama 저장소에 `bge-m3`가 있어도 같은 방식으로 동작합니다. 다른 임베딩 모델을 직접 받아 쓰려면 `--embedding-model`에 해당 모델명을 지정합니다.

### 외부 AI API 연결

외부 전송이 허용된 PC에서 OpenAI API 또는 JSON 모드를 지원하는 OpenAI 호환 게이트웨이를 연결할 수 있습니다.

1. 앱을 실행하고 우측 상단의 **API 설정**을 엽니다. OpenAI 주소와 예시 모델 `gpt-4.1-mini`가 미리 입력됩니다.
2. API 키를 입력합니다. 다른 제공자는 해당 제공자의 전체 HTTPS 요청 주소와 모델명을 입력합니다.
3. **연결 테스트**로 키·주소·모델을 확인할 수 있습니다. 테스트는 정해진 문구만 보내며 메일이나 검색어를 사용하지 않습니다.
4. **적용**하면 AI 질문의 답변은 항상 외부 API가 작성합니다. 임베딩(의미 검색)은 계속 이 PC의 로컬 모델이 담당합니다.

GUI에서 입력한 설정과 키는 앱 실행 중에 유지됩니다. 브라우저 새로고침 후에도 사용할 수 있으며, 앱을 종료하면 지워집니다. **연결 해제**로 실행 중인 연결도 즉시 지울 수 있습니다.

#### 환경 변수로 미리 설정하기

아래 예시는 OpenAI의 `gpt-4.1-mini`를 사용합니다. 키는 PowerShell의 입력 화면에 붙여 넣고 Enter로 입력합니다.

```powershell
$env:MAILSEARCH_EXTERNAL_MODEL = "gpt-4.1-mini"
$secret = Read-Host "OpenAI API 키" -AsSecureString
$env:MAILSEARCH_EXTERNAL_API_KEY = [System.Net.NetworkCredential]::new("", $secret).Password
try {
    .\start-windows.cmd --readpst "C:\msys64\mingw64\bin\readpst.exe"
} finally {
    Remove-Item Env:MAILSEARCH_EXTERNAL_API_KEY -ErrorAction SilentlyContinue
}
```

- 기본 주소는 `https://api.openai.com/v1/chat/completions`입니다. 다른 제공자를 쓰려면 `MAILSEARCH_EXTERNAL_URL`에 `/chat/completions`(OpenAI 호환) 또는 `/messages`(Anthropic Messages 호환)로 끝나는 전체 HTTPS 주소를 지정하고, 해당 제공자의 모델명과 키를 사용합니다. 두 형식 모두 Bearer 인증과 JSON 객체 응답을 지원해야 합니다. 로컬 AI도 함께 쓰려면 기존 `--model`과 `--embedding-model` 옵션을 추가합니다.
- Anthropic Messages 호환 예시는 Claude Code가 사용하는 z.ai 엔드포인트입니다. `MAILSEARCH_EXTERNAL_URL`에 `https://api.z.ai/api/anthropic/v1/messages`를 지정하고 모델은 `glm-5.3`처럼 해당 계정의 모델명과 키를 사용합니다.
- **외부 API → AI에게 질문**(버튼 또는 `Ctrl+Enter`)을 실행하면 메일·첨부에서 뽑은 발췌문 최대 5개(각 800자)가 질문과 함께 자동으로 전송됩니다. 전송 주소와 모델은 **API 설정**에서 확인·변경합니다.
- 검색, PST 색인, 이전 질문 맥락과 계정·메일의 별도 메타데이터는 로컬에서 처리합니다. 발췌문 안에 이미 포함된 이름, 주소, 본문 정보는 그대로 표시되므로 전송 전에 확인하세요. 앱은 GUI에서 입력받은 키를 설정 조회나 검색 응답으로 다시 보내지 않습니다.
- 외부 설정을 비워 두면 로컬 모드로 실행됩니다. 외부 API를 쓰면 제공자의 요금과 데이터 처리 정책이 적용됩니다.

## 해보기

- **검색**은 품번, 발신자, 본문, 첨부명을 단어와 의미로 함께 찾습니다. 키보드에서 `/`를 누르면 검색창으로 이동합니다. `Enter`는 일반 검색(단어+의미), `Ctrl+Enter`는 AI 질문을 실행합니다. 결과마다 왜 걸렸는지 일치 단어와 임베딩 유사도가 함께 표시됩니다. 단어를 정확히 포함한 메일만 찾으려면 `"납품"`처럼 따옴표로 묶습니다. `"A-17" & "승인"`은 둘 다 포함된 메일, `"A-17" or "B-19"`는 하나라도 포함된 메일을 찾습니다.
- 검색어를 비우고 검색하면 등록한 보관함의 최신 메일을 보여 줍니다. **검색 범위**를 펼쳐 주제·본문·발신자·수신자·첨부 여부·기간을 지정하거나 초기화할 수 있습니다.
- 보관함 목록은 마지막 색인 뒤 바뀐 PST 파일을 **갱신 대상 PST**로 알려 줍니다. 갱신을 누르면 바뀐 PST만 다시 읽고 새 메일만 색인·임베딩합니다.
- **문서 검색**은 등록한 폴더의 PDF(페이지), DOCX(본문), XLSX(시트·셀), TXT를 메일과 한 검색창에서 함께 찾습니다. 인용을 누르면 시트와 셀 위치(예: `양산 BOM!F18`), PDF 페이지가 표시됩니다. 구형 DOC·XLS는 LibreOffice가 설치된 경우 읽기 전용으로 변환해 색인합니다.
- **AI에게 질문**(`Ctrl+Enter` 또는 버튼)은 두 단계로 동작합니다. 처음 누르면 단어+의미 검색 결과를 매치 문장 강조와 함께 보여 주고, 같은 질문으로 한 번 더 누르면 근거를 모아 외부 API가 정리합니다. 메일에 날짜와 승인 문구가 함께 명시된 납품 일정 질문은 원문에서 바로 답을 만들 수 있습니다. 답변에는 확인 가능한 인용이 붙습니다. 예를 들어 “한빛 건 자재 늦어서 밀린 거, 결국 언제로 정해졌지?”처럼 질문할 수 있습니다.
- **설계 이력 질문**은 검색창 아래 **설계 이력 질문**에 체크한 뒤 같은 두 단계로 실행합니다. 메일·시험 보고서·BOM·기준서 발췌를 항목별로 모으고(첫 검색이 부족하면 한 번 더 검색), 주변 문단까지 포함해 외부 API가 결정 내용·변경 이유·승인 범위(시제품/양산)·반영 상태로 나눠 정리합니다. 확인되지 않은 항목은 "확인되지 않습니다"로 명시되고, 시제품 승인 근거로 양산 승인을 주장하면 해당 주장은 검증에서 걸러집니다.
- 설계 이력 질문에서 **수치 차이·합계·최댓값**("차이는 몇 mm?")은 원문 저장값으로 바로 계산해 두 위치를 각각 인용합니다. **답변 범위**로 특정 보관함만 쓰도록 좁힐 수 있고, 답변의 **결정 저장**을 누르면 사이드바에 쌓이며 근거 문서가 바뀌면 **재검토 대상**으로 표시됩니다.
- 재정렬 실험: 앱 실행 환경에 `MAILSEARCH_DESIGN_RERANK=1`을 지정하거나 요청에 `rerank`를 붙이면 같은 API가 근거 후보를 다시 정렬합니다. 근거 검색 품질은 `py -3 -m tools.eval_design`으로 합성 10개 시나리오에 대해 측정할 수 있습니다.
- **스캔 PDF**는 텍스트가 거의 없는 페이지를 골라 Tesseract(kor+eng)가 설치된 경우에만 OCR해 `N페이지(OCR)` 위치로 색인합니다. 미설치 시 텍스트 페이지만 색인됩니다.
- 인용을 누르면 발신자·발송 시각·출처 폴더와 함께 원문 구절을 볼 수 있습니다. 첨부 인용은 추출된 텍스트와 페이지·셀 위치를 보여 줍니다.

합성 메일만으로 확인하려면 아래 명령을 사용해 생성된 폴더를 보관함으로 등록합니다.

```powershell
py -3 tools/demo.py ".\demo-mail"
```

## 운영 특성

- 보관함 등록은 원본을 읽으며, PST 추출본과 색인은 사용자 로컬 데이터 폴더에 저장합니다. Windows 기본값은 `%LOCALAPPDATA%\OutlookMailSearch`입니다. 관리자와 협의한 보호된 디스크 경로를 `--data`로 지정할 수 있습니다. 색인은 원문과 같은 권한으로 보호해야 합니다.
- PST는 먼저 로컬 EML로 내보냅니다. 스풀이 완성되면 재개 시 재사용합니다. PST 추출 단계 자체가 중단되면 그 단계는 처음부터 다시 실행합니다. 이후 메일·첨부·임베딩은 커밋된 지점에서 재개합니다. 처음 등록할 때 EML 추출이 끝나고 색인된 범위부터 검색됩니다.
- 갱신할 때는 새로 생기거나 달라진 메일만 색인·첨부 추출·임베딩합니다. 이전 색인과 동일한 메일은 결과를 그대로 가져오고, PST를 모아 둔 폴더에서는 이전과 같은 PST 파일을 다시 추출하지 않습니다.
- 디스크에는 임시 EML과 검색 DB가 동시에 필요합니다. 파일 갱신에는 이전 색인과 새 색인이 함께 존재하는 구간이 있습니다. 실제 여유 공간은 대표 PST를 사용해 측정합니다.
- 텍스트 PDF, DOCX, XLSX, TXT의 본문을 읽습니다. 스캔본, HWP/HWPX, 암호화 첨부는 파일명으로 검색됩니다. 첨부당 25MiB·추출 텍스트 30만 자 제한이 있으며 누락 또는 일부 처리 상태가 표시됩니다. XLSX는 저장된 값의 시트와 셀 주소를 보여 주며 수식을 다시 계산하지 않습니다. 독립 문서 등록도 같은 형식 지원과 한도를 따릅니다.
- 원문 보기에는 정규화된 메일 텍스트와 첨부 발췌가 표시됩니다. HTML 스크립트, 외부 이미지, 매크로는 실행되지 않습니다. 임시 파일과 DB는 등록 해제 시 정리됩니다. SSD·백업의 물리 데이터 보존 정책은 업무 PC 보안 정책을 따릅니다.

## PST에서 가져온 메일이 0건일 때

- 먼저 Outlook에서 등록한 **바로 그 PST 파일**을 열어 받은 편지함과 하위 폴더에서 메일이 보이는지 확인합니다. 보관함을 등록할 때는 폴더 경로 대신 `C:\MailArchive\archive.pst`처럼 파일명까지 입력합니다.
- Outlook을 닫고 앱에서 해당 보관함의 **다시 색인**을 누릅니다. 앱은 추출된 메일이 없거나 파일을 읽을 수 없을 때 보관함 아래에 상태와 점검 항목을 표시합니다.
- Outlook에서 메일이 보이는데도 추출되지 않으면, 새 PST에 메일이 포함된 폴더와 하위 폴더를 내보낸 뒤 새 파일을 등록합니다. [Outlook PST 내보내기 점검](https://support.microsoft.com/en-us/outlook/fix-problems-importing-an-outlook-pst-file)

## 검증

```text
python -m unittest discover -s tests -v
py -3 -m tools.eval_design            # 설계 이력 근거 적중률 측정(합성 10 시나리오)
```

테스트는 합성 메일과 Ollama 응답을 흉내 낸 로컬 서버로 수행합니다. `MAILSEARCH_TEST_PST`와 `MAILSEARCH_TEST_READPST`를 지정하면 별도 PST 실물 테스트도 실행합니다. PRD의 50GB 검색 지연·정확도 합격 기준은 대표 Windows 장비, 실제 로컬 모델, 승인된 사내 시험 자료로 측정해야 합니다.

검색 속도는 `python -m tools.benchmark --count 50000`으로 합성 데이터에서 재현할 수 있습니다. 이 시험은 검색 DB의 응답을 측정합니다. PST 추출, 화면 표시, AI 추론 시간은 별도입니다.

| 개발 환경 검증 | 결과 |
|---|---|
| 공개 소형 PST 테스트 파일 | 메일 6건 추출, 원본 SHA-256 일치 |
| 30만 건 합성 메일 DB | 품번 검색 P95 0.9ms, 빈도가 높은 한국어 검색어 P95 580ms |
| 화면 동작 | DOM 환경에서 등록, 검색, 답변 인용, 인용 강조, 첨부 보기 확인 |
| 실제 로컬 모델 연결 | 합성 메일에 BGE-M3 의미 검색 적용. 납품 승인 메일이 있으면 원문을 인용해 4월 25일로 답했고, 승인 메일을 제거한 뒤 Qwen3가 확인 가능한 최종 승인일을 제시하지 않음. 후자 처리에는 CPU에서 약 23초 소요 |
