# AGENTS.md — 에이전트용 프로젝트 가이드

이 문서는 이 저장소를 처음 보는 AI 에이전트가 안전하고 빠르게 기여하도록 돕는다. 요구사항 전문은 `docs/PRD.md`, 사용법은 `README.md`, 변경 이력은 `CHANGELOG.md` 참고.

## 프로젝트 요약

Outlook PST 보관 메일을 로컬에서 색인해 검색하고, 원문 인용이 붙은 AI 답변을 제공하는 Windows 우선 루프백 웹앱. **Python 표준 라이브러리만 사용**(pip 의존성 추가 금지). 프론트엔드는 프레임워크 없는 바닐라 JS.

## 실행·검증

```powershell
.\start.vbs                                  # 창 없는 실행(배포용 기본)
.\start-windows.cmd --readpst ... [옵션]      # 콘솔 창에서 직접 실행
py -3 -m unittest discover -s tests          # 전체 테스트(변경 후 반드시 통과 확인)
```

서버 인자: `--port`(기본 8765), `--readpst`, `--pdftotext`, `--model`(로컬 생성), `--embedding-model`, `--ollama-port`(기본 11435). 외부 AI는 `MAILSEARCH_EXTERNAL_URL/MODEL/API_KEY` 환경 변수 또는 GUI API 설정. `LITELLM_API_KEY`를 설정하면 HD AIC의 bge-m3로 색인·검색용 벡터를 만든다. `--embedding-model`을 명시하면 PC에 설치한 Ollama 모델을 사용한다.

**종료 동작**: 페이지가 2.5초마다 `/api/ping`을 보내고 `main()`의 `watch_page` 스레드가 12초 이상 무응답이면 서버를 종료한다(탭을 모두 닫으면 앱이 죽는다. 새로고침은 끊기지 않게 임계값이 넉넉하다). `--no-browser`로 실행하면 이 감시가 켜지지 않는다.

## 모듈 지도

| 경로 | 책임 |
|---|---|
| `mailsearch/server.py` | 루프백 HTTP 서버, 라우팅, 세션 토큰, CSP, 외부 API 설정/전송, 검색 질의 하이브리드 병합 |
| `mailsearch/indexer.py` | 백그라운드 가져오기(단일 워커 스레드), readpst 실행, 버전 원자 스왑, 증분 갱신 |
| `mailsearch/store.py` | SQLite 스키마(sources/mails/passages/terms/vectors/pst_files), 키워드·의미·정확 검색 |
| `mailsearch/answer.py` | 루프백 Ollama 연결(생성·임베딩, 필요 시 `ensure_server`가 백그라운드 시작), 근거 검색 `evidence_for`, 답변 검증 `validate` |
| `mailsearch/embedding.py` | HD AIC HTTPS 임베딩 API 요청·응답 검증 |
| `mailsearch/external.py` | 외부 AI 전송(OpenAI 호환 + Anthropic Messages 호환), 발췌문 리뷰 토큰 |
| `mailsearch/extract.py` | EML 파싱, 문서 텍스트 추출(PDF/DOCX/XLSX/TXT, 구형 DOC/XLS는 LibreOffice 읽기 전용 변환) |
| `mailsearch/static/` | UI(`index.html`/`app.js`/`style.css`) |
| `vendor/readpst-win64/` | MSYS2 libpst 0.6.76 휴대용 번들(Windows, DLL 의존성 포함) |
| `setup-windows.ps1` | 첫 실행 환경 구성(파이썬 확인/설치, 번들 readpst 확인) |

## 데이터 모델·색인 흐름

PST --readpst--> EML 스풀 --> `parse_mail` --> `mails`/`passages`/`terms`(FTS5) --> `vectors`(임베딩, 모델 또는 API 주소와 모델이 키). 문서 폴더·파일(PDF·DOCX·XLSX·TXT, PRD-02)은 `extract_document`가 위치(페이지·본문·시트!셀)를 보존해 같은 테이블에 색인한다. `sources.active`가 현재 검색 대상 버전이다. 갱신은 새 `pending` 버전을 완성한 뒤 스왑하고 이전 버전 행을 삭제한다(중단 시 이전 색인 유지).

**증분 갱신**: `pst_files` 테이블의 PST별 서명(크기·mtime)이 같으면 재추출 없이 이전 버전 행을 접두사로 복사(`carry_prefix`). 바뀐 PST도 메일 `fingerprint`(message_id·제목·본문 등 해시)가 같으면 색인·첨부·임베딩 행을 통째로 복사(`adopt_mail`)하고 새 메일만 처리한다.

## 검색 동작

- **전체/메일/문서/PCF 탭**: 전체 탭은 자료 유형을 결합한다. 나머지는 mails.kind로 제한한다. 보관함은 filters.source로 제한한다.
- **PCF 답변**: 전용 API는 PCF 문서로 범위를 고정한다. 파일명과 문서의 명시적 필드를 기준으로 필터링한다. COMMENT 번호가 같은 인접 구간으로 문맥을 확장하고, 발췌한 근거의 승인 범위·수치 단위·종결을 확인한다.
- **Enter(일반 검색)**: 키워드(FTS terms + 한글 bigram grams)와 의미(임베딩 유사도)를 병합(`merge_rows`). 결과에는 일치 단어(`matched`)·유사도(`similarity`)·매치 문장(`snippet`)이 붙고 UI가 단어를 강조한다.
- **따옴표 정확 검색**: `"단어"` — 해당 단어를 포함한 메일만. `"A" & "B"`는 모두, `"A" or "B"`는 하나라도. 파싱은 `store.quoted_plan`, 실행은 `store.exact_search`.
- **Ctrl+Enter(AI 근거 찾기)**: 답변 API에 보낼 질문과 발췌문을 먼저 표시한다. 사용자가 선택한 뒤 **선택한 근거로 답변**을 눌러 전송한다. 검토 토큰은 생성 경로 및 API 설정과 결합되며, 전송 직전 원본 접근을 다시 확인한다.

## 불변식 — 변경 시 반드시 유지

1. 웹 서버는 127.0.0.1 전용. `/api/*`는 `X-Mail-Session` 토큰 필요, Origin/Host 검증 유지.
2. 기본 답변 API는 검토한 발췌문(≤5개·각 800자)과 질문만 전송한다. 일반 메일·문서는 운영자가 설정한 `MAILSEARCH_INTERNAL_URL`과 일치할 때 검토한 발췌문과 표시된 메타데이터를 설정된 바이트 예산 내에서 보낸다. 구조화 Word PCF는 운영자가 설정한 `MAILSEARCH_PCF_INTERNAL_URL`과 정확히 일치하는 HTTPS 주소에서만 검토한 셀과 필요한 구조 정보를 별도 입력 예산으로 보낸다. HD AIC 임베딩은 환경에 키를 설정했을 때만 사용하며 색인 시 제목·첨부명·텍스트, 의미 검색 시 검색어를 고정 HTTPS 주소로 전송한다. API 키는 어떤 응답에도 반환하지 않는다.
3. 표준 라이브러리만 사용. readpst/pdftotext는 서브프로세스로만 호출.
4. API 키를 소스·저장소·로그에 기록 금지. 환경 변수 또는 실행 중 GUI 세션에서만.
5. 색인 원자성: 검색 가능 상태는 항상 직전 완성 버전을 유지한다.
6. `.cmd` 파일은 ASCII 전용(CP949 파싱 파손 방지). 한국어 메시지는 `.ps1`(UTF-8 BOM)이나 파이썬에서.

## 기여 규칙

- 파일당 1커밋. 커밋 메시지는 영어, 푸터 `Co-Authored-By: Claude <noreply@anthropic.com>`.
- UI 문구는 한국어, 코드·주석 식별자는 영어. 주변 코드 스타일을 따른다.
- 코드 변경 후 테스트 전체 통과를 확인한 뒤 커밋한다.
- 사용자가 확인할 만한 변화는 `CHANGELOG.md`에도 한 줄 추가한다.
