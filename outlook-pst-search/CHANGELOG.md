# 변경 이력

주요 변화만 기록한다. 전체 커밋은 `git log` 참고.

## 2026-11-21

### 검색
- Enter 일반 검색이 단어(FTS)+의미(임베딩) 하이브리드로 동작. 임베딩 서버가 없으면 키워드만.
- 따옴표 정확 검색 추가: `"단어"` 포함 메일만 검색, `"A" & "B"`(모두), `"A" or "B"`(하나라도) 연산.
- 검색 범위 필터 개편: 보관함·폴더 제거, 주제·본문·첨부 여부 추가(기존 발신자·수신자·기간 유지).

### AI 연결
- 사이드바에 로컬 AI·외부 API 연결 상태 표시(연결 시 모델명 표기).
- 외부 API 질문(Ctrl+Enter 포함)이 전송 검토 다이얼로그 없이 자동 전송. AI 답변의 "외부 API" 배지 제거.
- 중앙 임베딩 서버(NAS) 지원: `--embedding-url`, `start.cmd`의 `EMBEDDING_URL`/`EMBEDDING_MODEL` 설정. 추천 모델 `qwen3-embedding:4b`(2.5GB) / 여유 부족 시 `0.6b`(639MB).
- z.ai 등 Anthropic Messages 호환 게이트웨이 지속 개선(의미 검색 근거 사용).

### 성능
- 갱신 증분 색인: 서명이 같은 PST는 재추출 생략, 내용이 같은 메일은 색인·첨부·임베딩 행을 이전 버전에서 복사. 새 메일만 색인/임베딩.

### 배포·UI
- readpst 0.6.76 휴대용 번들(`vendor/readpst-win64`) 저장소 포함 — MSYS2 수동 설치 불필요.
- `setup-windows.cmd` 첫 실행 자동 구성(파이썬 winget 설치, 번들 readpst 확인, Ollama 선택), `start.cmd` 원클릭 실행.
- `Ctrl+Enter` AI 질문 단축키(Enter=일반 검색), "검색 시작하기" 카드 제거.
- 폴더 등록 시 내부 PST 자동 가져오기(빈/깨진 PST 건너뛰고 누락 표시).

## 초기 (업스트림)

- 오프라인 PST 검색 + 원문 인용 AI 답변 앱 초기 구조(루프백 서버, FTS5, Ollama 로컬 AI).
- 종이 공감 GUI, 외부 AI API 연결(GUI 설정·연결 테스트·세션 스코프).
- Anthropic Messages 게이트웨이 지원 및 z.ai 예시 문서화.
- 번호 붙은 readpst 추출본 처리와 빈 PST 진단.
