# 업무 자료 검색

Outlook 메일·업무 문서·선주 교신문(PCF)을 업무 PC에서 색인합니다. 선택한 자료를 검색하고, 답변 API로 보낼 근거를 검토한 뒤 인용으로 돌아가 원문을 확인할 수 있습니다.

[메일 검색 PRD](docs/PRD.md) · [메일·문서 통합 검색 PRD](docs/PRD-02-document-rag.md)

## Windows에서 실행

1. GitHub에서 **Code → Download ZIP**으로 내려받고 압축을 풉니다. 사내망에서 GitHub에 접속할 수 없으면 내려받은 소스와 필요한 의존성을 사내 반입 방식으로 옮깁니다.
2. **setup-windows.cmd**를 한 번 실행합니다. 파이썬(없으면 winget으로 설치)과 저장소에 포함된 PST 변환 도구 readpst를 확인합니다.
3. **start.vbs**를 두 번 클릭하면 창 없이 실행되고 브라우저에서 앱이 열립니다. 콘솔 로그가 필요하면 `start-windows.cmd`를 직접 실행합니다.
4. 열린 브라우저에서 **자료 추가**를 누른 뒤, Outlook을 닫고 로컬 디스크에 보관한 PST 경로를 등록합니다. 여러 파일은 한 번씩 등록하거나 PST를 모아 둔 폴더를 등록합니다. 폴더를 등록하면 그 안의 .eml 메일, 모든 PST, 문서 파일(PDF·DOCX·XLSX·TXT)을 함께 가져오며, 빈 PST나 읽을 수 없는 PST는 건너뛰고 누락으로 표시합니다. 문서 파일 하나만 따로 등록할 수도 있습니다. Outlook이 사용하는 데이터 파일 대신 닫혀 있는 PST 사본을 등록하면 파일 교체·갱신 시점을 제어하기 쉽습니다.
5. 브라우저에서 앱 탭을 모두 닫으면 앱도 종료됩니다(마지막 탭을 닫은 뒤 최대 10여 초 걸릴 수 있습니다). 콘솔로 실행한 경우 **Ctrl+C**로 종료합니다. 진행 중이던 색인은 다음 실행에서 **재개**할 수 있습니다.

인터넷이 막혀 setup-windows.cmd를 쓸 수 없거나 직접 준비하려면 FTS5가 포함된 **SQLite 3.42 이상**을 사용하는 **Python 3.12 이상**을 설치하세요. SQLite 버전은 `py -3 -c "import sqlite3; print(sqlite3.sqlite_version)"`으로 확인합니다. readpst는 저장소의 `vendor\readpst-win64\readpst.exe`를 그대로 쓰거나 [MSYS2 MinGW libpst](https://packages.msys2.org/packages/mingw-w64-x86_64-libpst)에서 직접 준비합니다. 텍스트 PDF 첨부를 색인하려면 `pdftotext`가 필요한데, Git for Windows가 설치된 PC에서는 그에 포함된 `pdftotext`를 자동으로 사용하므로 별도 설치가 없어도 됩니다. 없다면 [Poppler](https://poppler.freedesktop.org/)에서 준비합니다. PowerShell에서 앱 폴더로 이동하고 실행합니다. 도구가 PATH에 있으면 `--readpst`와 `--pdftotext`를 생략할 수 있습니다.

```powershell
.\start-windows.cmd --readpst "C:\msys64\mingw64\bin\readpst.exe" --pdftotext "C:\tools\poppler\bin\pdftotext.exe"
```

Linux에서도 SQLite 3.42 이상을 사용하는 Python이 필요합니다. `pst-utils`와 `poppler-utils`를 설치한 뒤 `python3 -m mailsearch`로 실행합니다. 파이썬 앱 자체는 표준 라이브러리만 사용합니다.

### HD AIC 사내 모델 설정

상단 **API 설정 → 임베딩 · HD AIC 모델**에서 사내 API를 사용하려면 LiteLLM 키를 한 번 입력합니다. 각 드롭다운에서 용도별 모델을 선택한 뒤 **연결 테스트 → 적용**을 누릅니다. 연결 테스트는 고정 문구를 사용합니다. GUI에 입력한 키는 앱을 종료하면 지워집니다. 실행 전에 환경 변수 `LITELLM_API_KEY`를 설정한 경우에는 해당 키를 불러오며, 드롭다운에서 모델을 추가한 뒤 키 칸을 비워 두고 적용할 수 있습니다.

| 용도 | 모델 | 앱에서 쓰는 범위 |
|---|---|---|
| 근거 답변 | Qwen3.8-27B, DeepSeek-V4.1-Flash-1017 | 사용자가 검토한 메일·문서·PCF 발췌로 답변 생성 |
| 의미 검색 | BGE-M3 | 로컬 Ollama 또는 사내 HD AIC API로 색인 텍스트와 검색어의 벡터 생성 |
| 후보 재정렬 | BGE-Reranker-v2-M3 | 검색어와 상위 24개 후보 텍스트를 함께 비교 |

처음에는 **Qwen3.8-27B + BGE-M3**에 재정렬을 추가해 일반 업무 검색을 평가합니다. 답변 모델은 한 번에 하나를 사용합니다. 같은 질문과 발췌문으로 두 모델의 답변·인용 정확도와 응답 시간을 비교할 수 있습니다.

- HD AIC 요청은 `api.model.hd-aic.com`으로 전송합니다. 임베딩은 색인 시 제목·첨부명·텍스트 조각과 의미 검색어를, 재정렬은 검색어와 후보 발췌를 사용합니다. 사내 답변 모델에는 전송 화면에서 사용자가 선택한 근거를 보냅니다.
- 임베딩 실행 위치를 바꿀 때 새 방식으로 색인한 적이 없는 보관함은 **갱신**을 누릅니다. 로컬과 사내 API 벡터는 각각 저장하므로 이미 색인한 방식으로 돌아갈 때 기존 벡터를 다시 씁니다. 답변 모델이나 재정렬 설정 변경은 기존 색인에 바로 적용됩니다. 기존 키워드 검색은 갱신 중에도 가능합니다.

### 로컬 AI 사용하기

**API 설정 → 임베딩 · HD AIC 모델 → 임베딩 실행 위치 → 로컬 Ollama · BGE-M3**를 선택합니다. PC에 [Ollama](https://docs.ollama.com/)와 BGE-M3가 설치돼 있어야 합니다. 인터넷 연결이 허용되는 환경에서 `ollama pull bge-m3`로 준비할 수 있습니다. 로컬 임베딩에는 API 키가 필요하지 않습니다. 사내 답변이나 재정렬을 함께 켜면 해당 기능은 사내 API를 사용합니다.

시작 명령으로 지정하려면 `--embedding-model bge-m3`를 씁니다. 로컬 답변도 설정하려면 아래처럼 `--model`을 추가합니다. 앱이 시작한 Ollama는 앱을 종료할 때 함께 종료됩니다.

```powershell
.\start-windows.cmd --model qwen3:4b --embedding-model bge-m3
```

### 외부 AI API 연결

외부 전송이 허용된 PC에서 OpenAI API 또는 JSON 모드를 지원하는 OpenAI 호환 게이트웨이를 연결할 수 있습니다.

1. 앱을 실행하고 우측 상단의 **API 설정 → 기타 답변 API**를 엽니다. OpenAI 주소와 예시 모델 `gpt-4.1-mini`가 미리 입력됩니다.
2. API 키를 입력합니다. 다른 제공자는 해당 제공자의 전체 HTTPS 요청 주소와 모델명을 입력합니다.
3. **연결 테스트**로 키·주소·모델을 확인할 수 있습니다. 테스트는 정해진 문구만 보내며 메일이나 검색어를 사용하지 않습니다.
4. **적용**하면 근거 검토 화면에 답변 API 주소와 모델이 표시됩니다. 질문과 발췌문은 **선택한 근거로 답변**을 누른 뒤 전송합니다. 의미 검색은 위의 임베딩 설정을 사용합니다.

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
- **AI 근거 찾기**(버튼 또는 Ctrl+Enter)는 전송 전에 질문과 발췌문을 표시합니다. 기본 설정에서는 선택한 발췌문 최대 5개(각 800자)와 질문이 API로 전달됩니다.
- 검색, PST 색인, 이전 질문 맥락은 로컬에서 처리합니다. 기본 설정에서는 별도 메일 메타데이터를 보내지 않습니다. 발췌문 안에 이미 포함된 이름, 주소, 본문 정보는 그대로 표시되므로 전송 전에 확인하세요. 앱은 GUI에서 입력받은 키를 설정 조회나 검색 응답으로 다시 보내지 않습니다.
- 답변 API 설정과 관계없이 로컬 검색을 사용할 수 있습니다. 외부 API를 쓰면 제공자의 요금과 데이터 처리 정책이 적용됩니다.

### 사내 API에서 일반 메일·문서 문맥 확장

PowerShell에서 아래 주소를 사내 HTTPS API의 실제 주소로 바꾼 뒤 앱을 실행합니다. **API 설정**의 주소가 일치하면 적용됩니다. 기존 검색 색인도 사용할 수 있습니다.

```powershell
$env:MAILSEARCH_INTERNAL_URL = "https://ai.intranet.example/v1/chat/completions"
$env:MAILSEARCH_INTERNAL_INPUT_BYTES = "24000"
.\start-windows.cmd
```

- 일반 메일·문서에서 질문과 관련된 문단, 연결된 회신, 본문 끝과 인접 문단을 후보로 찾습니다. 엑셀은 일치한 행과 같은 시트의 처음 두 행 및 앞 행을 함께 검토할 수 있습니다. 여러 표가 있는 시트에서는 실제 머리글과 단위를 원문에서 확인하세요.
- **AI 근거 찾기**에서 발췌 원문과 함께 전송할 제목·발신자·시각·첨부명·위치를 확인합니다. 선택한 근거만 한 번 전송합니다. 전체 질문·근거 JSON의 기본 한도는 UTF-8 24,000바이트, 최대 16개 발췌문입니다. 설정 범위는 3,000~96,000바이트입니다. 시스템 지시문과 답변에 필요한 모델 토큰은 별도로 확보하세요.
- 답변은 인용 주변 문장의 금액·단위·요일, 명시된 담당 조직, 조건과 요청·확정 표현을 대조합니다. 같은 보관함·메일 주제에서 서로 다른 확정 납품일이 확인되면 두 원문을 함께 표시합니다.
- PCF 전체 이력 전송에는 아래의 별도 PCF 설정을 사용합니다. HD AIC를 사내 답변 모델로 선택하면 해당 사내 프로필을 자동 적용합니다.

## 해보기

- **검색**은 품번, 발신자, 본문, 첨부명을 단어와 의미로 함께 찾습니다. 키보드에서 `/`를 누르면 검색창으로 이동합니다. `Enter`는 일반 검색(단어+의미), `Ctrl+Enter`는 AI 질문을 실행합니다. 결과마다 왜 걸렸는지 일치 단어와 임베딩 유사도가 함께 표시됩니다. 단어를 정확히 포함한 메일만 찾으려면 `"납품"`처럼 따옴표로 묶습니다. `"A-17" & "승인"`은 둘 다 포함된 메일, `"A-17" or "B-19"`는 하나라도 포함된 메일을 찾습니다.
- **전체 / 메일 / 문서 / PCF** 탭에서 자료 종류를 고릅니다. **검색 범위**의 보관함을 선택하면 특정 자료 폴더 또는 파일로 좁힐 수 있습니다. 검색어를 비우면 선택 범위의 최신 자료가 표시됩니다.
- 보관함에서 **자료 추가**를 눌러 PST·EML·문서 파일이나 폴더를 등록합니다. 자료 카드에는 종류별 건수와 누락·일부 색인 상태가 표시됩니다. 마지막 색인 뒤 바뀐 PST는 갱신 대상으로 표시합니다.
- **문서 검색**은 PDF(페이지), DOCX(본문), XLSX(시트·셀), TXT를 찾습니다. 구형 DOC·XLS는 LibreOffice가 설치된 경우 읽기 전용으로 변환합니다. 인용에서 해당 파일과 페이지·셀 위치를 확인할 수 있습니다.
- **PCF 탭**에서 번호 또는 본문으로 검색합니다. 파일명에 PCF가 있는 Word 문서의 No. / Owner / Yard 표를 COMMENT별로 색인합니다. 검색 결과를 열면 해당 COMMENT의 셀 원문과 빈 회신을 확인할 수 있습니다. 번호가 여러 표나 파일에서 겹치면 후보를 선택합니다. 기존 PCF 보관함은 **갱신**하면 표를 다시 판독합니다.

- **스캔 PDF**의 텍스트가 적은 페이지는 Poppler 렌더러와 Tesseract가 설치된 경우 OCR 대상으로 처리합니다. 인용에는 OCR 페이지 위치가 표시되며, 후보 중 읽지 못한 페이지는 일부 색인으로 표시합니다. 누락이 표시되면 파일과 도구를 확인하고 다시 색인할 수 있습니다.
- 코드 관계도인 graphify-out은 개발 참고 자료입니다. git archive로 만든 배포용 아카이브에서는 제외됩니다.

### Word PCF 전체 이력 답변

- DOCX는 Word 표 XML에서 병합된 번호와 행별 Owner·YARD를 추출합니다. DOC는 설치된 LibreOffice로 임시 사본을 DOCX로 변환합니다. 사용 중인 Word에서 DOCX 사본으로 저장한 다음 표를 대조하고 등록할 수도 있습니다.
- ‘PCF 표 확인 필요’ 또는 ‘PCF 표를 찾지 못함’이 표시되면 표 머리글·병합 및 변환본을 확인합니다. 중첩 표와 이미지 셀은 원본에서 확인해야 합니다.
- HD AIC 답변 모델은 사내 설정에서 고릅니다. 그 외 제공자는 사내 운영자가 `MAILSEARCH_PCF_INTERNAL_URL`에 허용할 정확한 HTTPS API 주소를 설정합니다. 앱의 **API 설정**에서 같은 주소와 모델을 연결합니다. 주소 예시는 `https://ai.intranet.example/v1/chat/completions`입니다. `MAILSEARCH_PCF_INPUT_BYTES` 기본값은 `12000`이며 모델 입력 한도에 맞춰 `3000`~`96000` 범위에서 조정할 수 있습니다. 기존 평문 전송 프로필의 제한은 발췌문 5개·각 800자입니다.
- PCF 탭에서 질문하고 전송 검토 화면에 표시된 셀을 확인합니다. 긴 COMMENT는 행 단위로 묶고, 개별 셀이 너무 크면 원문 오프셋을 유지하며 분할합니다. 선택한 셀이 모두 처리된 경우 검증된 인용을 다시 대조해 답변을 합칠 수 있습니다. 답변의 인용에서 선택된 셀을 확인합니다.

합성 메일만으로 확인하려면 아래 명령을 사용해 생성된 폴더를 보관함으로 등록합니다.

```powershell
py -3 tools/demo.py ".\demo-mail"
```

## 운영 특성

- 보관함 등록은 원본을 읽으며, PST 추출본과 색인은 사용자 로컬 데이터 폴더에 저장합니다. Windows 기본값은 `%LOCALAPPDATA%\OutlookMailSearch`입니다. 관리자와 협의한 보호된 디스크 경로를 `--data`로 지정할 수 있습니다. 색인은 원문과 같은 권한으로 보호해야 합니다.
- PST는 먼저 로컬 EML로 내보냅니다. 스풀이 완성되면 재개 시 재사용합니다. PST 추출 단계 자체가 중단되면 그 단계는 처음부터 다시 실행합니다. 이후 메일·첨부·임베딩은 커밋된 지점에서 재개합니다. 처음 등록할 때 EML 추출이 끝나고 색인된 범위부터 검색됩니다.
- 갱신할 때는 새로 생기거나 달라진 메일만 색인·첨부 추출·임베딩합니다. 이전 색인과 동일한 메일은 결과를 그대로 가져오고, PST를 모아 둔 폴더에서는 이전과 같은 PST 파일을 다시 추출하지 않습니다.
- 디스크에는 임시 EML과 검색 DB가 동시에 필요합니다. 파일 갱신에는 이전 색인과 새 색인이 함께 존재하는 구간이 있습니다. 실제 여유 공간은 대표 PST를 사용해 측정합니다.
- 텍스트 PDF, DOCX, XLSX, TXT의 본문을 읽습니다. HWP/HWPX와 암호화 첨부는 파일명으로 검색됩니다. 스캔본은 OCR 도구를 사용할 수 있는 환경에서 내용을 색인합니다. 첨부당 25MiB·추출 텍스트 30만 자 제한이 있으며 누락 또는 일부 처리 상태가 표시됩니다. XLSX는 저장된 값의 시트와 셀 주소를 보여 주며 수식을 다시 계산하지 않습니다. 독립 문서 등록도 같은 형식 지원과 한도를 따릅니다.
- 원문 보기에는 정규화된 메일 텍스트와 첨부 발췌가 표시됩니다. HTML 스크립트, 외부 이미지, 매크로는 실행되지 않습니다. 임시 파일과 DB는 등록 해제 시 정리됩니다. SSD·백업의 물리 데이터 보존 정책은 업무 PC 보안 정책을 따릅니다.

## PST에서 가져온 메일이 0건일 때

- 먼저 Outlook에서 등록한 **바로 그 PST 파일**을 열어 받은 편지함과 하위 폴더에서 메일이 보이는지 확인합니다. 보관함을 등록할 때는 폴더 경로 대신 `C:\MailArchive\archive.pst`처럼 파일명까지 입력합니다.
- Outlook을 닫고 앱에서 해당 보관함의 **다시 색인**을 누릅니다. 앱은 추출된 메일이 없거나 파일을 읽을 수 없을 때 보관함 아래에 상태와 점검 항목을 표시합니다.
- Outlook에서 메일이 보이는데도 추출되지 않으면, 새 PST에 메일이 포함된 폴더와 하위 폴더를 내보낸 뒤 새 파일을 등록합니다. [Outlook PST 내보내기 점검](https://support.microsoft.com/en-us/outlook/fix-problems-importing-an-outlook-pst-file)

## 검증

```text
python -m unittest discover -s tests -v
```

테스트는 합성 메일과 Ollama·HD AIC 모의 응답으로 수행합니다. `MAILSEARCH_TEST_PST`와 `MAILSEARCH_TEST_READPST`를 지정하면 별도 PST 실물 테스트도 실행합니다. PRD의 50GB 검색 지연·정확도 합격 기준은 대표 Windows 장비, 실제 로컬 모델, 승인된 사내 시험 자료로 측정해야 합니다.

검색 속도는 `python -m tools.benchmark --count 50000`으로 합성 데이터에서 재현할 수 있습니다. 보관함 필터의 의미 검색은 `python -m tools.benchmark --semantic-scope --count 28000 --repetitions 20`으로 재현합니다. 벡터 후보가 겹치도록 만든 합성 자료에서 두 보관함 각각의 상위 20건 반환 수와 예열 후 P95를 출력합니다. 이 시험은 검색 DB의 응답을 측정합니다. PST 추출, 화면 표시, AI 추론 시간은 별도입니다.

| 개발 환경 검증 | 결과 |
|---|---|
| 공개 소형 PST 테스트 파일 | 메일 6건 추출, 원본 SHA-256 일치 |
| 30만 건 합성 메일 DB | 품번 검색 P95 0.9ms, 빈도가 높은 한국어 검색어 P95 580ms |
| 화면 동작 | DOM 환경에서 등록, 검색, 답변 인용, 인용 강조, 첨부 보기 확인 |
| 실제 로컬 모델 연결 | 합성 메일에 BGE-M3 의미 검색 적용. 납품 승인 메일이 있으면 원문을 인용해 4월 25일로 답했고, 승인 메일을 제거한 뒤 Qwen3가 확인 가능한 최종 승인일을 제시하지 않음. 후자 처리에는 CPU에서 약 23초 소요 |
