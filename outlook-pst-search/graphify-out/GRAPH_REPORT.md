# Graph Report - outlook-pst-search  (2026-11-22)

## Corpus Check
- Corpus is ~26,535 words - fits in a single context window. You may not need a graph.

## Summary
- 357 nodes · 788 edges · 14 communities (11 shown, 1 thin omitted)
- Extraction: 96% EXTRACTED · 4% INFERRED · 0% AMBIGUOUS · INFERRED: 34 edges (avg confidence: 0.88)
- Token cost: 11,000 input · 6,200 output

## Community Hubs (Navigation)
- 외부 AI 전송
- 문서·첨부 텍스트 추출
- PRD·문서 지식
- 검색 저장소·FTS
- 통합 테스트
- 색인 파이프라인
- 근거 검색·답변 검증
- 웹 UI (app.js)
- 로컬 Ollama 연결
- bge-m3 모델 매니페스트
- PST 테스트 데이터
- 패키지 진입점

## God Nodes (most connected - your core abstractions)
1. `Integration` - 40 edges
2. `AGENTS.md — 에이전트용 프로젝트 가이드` - 32 edges
3. `Store` - 29 edges
4. `Indexer` - 27 edges
5. `ExternalAI` - 19 edges
6. `await_source()` - 18 edges
7. `Handler` - 17 edges
8. `PRD 02 — 메일과 설계 문서를 함께 찾는 사내 RAG` - 17 edges
9. `LocalAI` - 13 edges
10. `extract_bytes()` - 13 edges

## Surprising Connections (you probably didn't know these)
- `원문 검증·인용 품질 기준` --rationale_for--> `validate()`  [INFERRED]
  docs/PRD.md → mailsearch/answer.py
- `AGENTS.md — 에이전트용 프로젝트 가이드` --references--> `validate()`  [EXTRACTED]
  AGENTS.md → mailsearch/answer.py
- `enron.pst — 대용량 실전 검색 벤치마크 PST` --semantically_similar_to--> `tools/benchmark.py — 검색 속도 재현`  [INFERRED] [semantically similar]
  test_pst/README.md → README.md
- `Integration` --uses--> `LocalAI`  [INFERRED]
  tests/test_mailsearch.py → mailsearch/answer.py
- `Integration` --uses--> `ExternalAI`  [INFERRED]
  tests/test_mailsearch.py → mailsearch/external.py

## Import Cycles
- None detected.

## Hyperedges (group relationships)
- **하이브리드 검색 파이프라인(키워드 FTS5 + 임베딩 의미 병합)** — mailsearch_server, mailsearch_store, mailsearch_server_mergerows, mailsearch_static_app, docs_prd_hybrid_search [INFERRED 0.85]
- **PCF 선주 교신문 답변 흐름** — mailsearch_server_pcf_retrieval, mailsearch_server_numeric_answer, mailsearch_server_pcf_instructions, mailsearch_external, mailsearch_static_index [INFERRED 0.85]
- **Ctrl+Enter 두 단계 AI 질의응답 흐름** — mailsearch_static_app_stagedquery, mailsearch_server_mergerows, mailsearch_answer_evidencefor, mailsearch_answer_validate, mailsearch_external [INFERRED 0.85]

## Communities (14 total, 1 thin omitted)

### Community 0 - "외부 AI 전송"
Cohesion: 0.07
Nodes (18): map_citations(), prepare(), Materialize the bundled bge-m3 under models_root and return it. The large model…, Excerpt, ExternalAI, ExternalRequestError, Optional HTTPS generation with explicit review of each outbound request., Review (+10 more)

### Community 1 - "문서·첨부 텍스트 추출"
Cohesion: 0.07
Nodes (29): HTMLParser, clean(), convert_legacy(), decode(), extract_attachment(), extract_bytes(), extract_document(), local_name() (+21 more)

### Community 2 - "PRD·문서 지식"
Cohesion: 0.07
Nodes (43): AGENTS.md — 에이전트용 프로젝트 가이드, CHANGELOG.md — 변경 이력, PRD — 사내 Outlook 메일 검색·근거 기반 AI 질의응답 v1.0, PRD 02 — 메일과 설계 문서를 함께 찾는 사내 RAG, 주장별 원문 검증, 메일+설계 문서 통합 사내 RAG 개념, Double-Bench — 여러 근거가 필요한 문서 QA 벤치마크, 근거 예산 — 발췌 문자량 관리(5×800자/5×2,400자) (+35 more)

### Community 3 - "검색 저장소·FTS"
Cohesion: 0.09
Nodes (20): document_identity(), fingerprint(), fts_query(), grams(), index_text(), like_prefix(), now(), Store immutable mail versions and search their locally indexed passages. (+12 more)

### Community 4 - "통합 테스트"
Cohesion: 0.14
Nodes (7): skipUnless, await_source(), Integration, A mixed corpus: one mail plus three documents., 메일 탭은 문서를, 문서 탭은 메일을 검색하지 않는다., 파일명에 PCF가 들어간 선주 교신문은 PCF 탭으로 분리되고 전용 지침으로 답변한다., 사이드바 섹션 구분을 위한 소스별 종류 카운트.

### Community 5 - "색인 파이프라인"
Cohesion: 0.12
Nodes (15): Exception, local_path(), Resolve local files and protect a PST against concurrent Windows writes., stable_input(), document_date(), ImportIssue, Indexer, Paused (+7 more)

### Community 6 - "근거 검색·답변 검증"
Cohesion: 0.13
Nodes (22): delivery_date_answer(), evidence_for(), merge_rows(), numeric_answer(), pcf_retrieval(), public_row(), Connect to a local model and accept only answers backed by retrieved quotes., Design-history evidence: first pass, one follow-up when thin, neighbor context. (+14 more)

### Community 7 - "웹 UI (app.js)"
Cohesion: 0.12
Nodes (32): addControls, ALL_FILTERS, call(), changeSettings(), dateFormat, element(), form, formatDate() (+24 more)

### Community 8 - "로컬 Ollama 연결"
Cohesion: 0.30
Nodes (5): LocalAI, ollama_binary(), Start a hidden Ollama server on demand; only ours to stop., source_text(), RuntimeError

### Community 9 - "bge-m3 모델 매니페스트"
Cohesion: 0.25
Nodes (7): config, digest, mediaType, size, layers, mediaType, schemaVersion

### Community 10 - "PST 테스트 데이터"
Cohesion: 0.40
Nodes (5): test_pst/README.md — PST 테스트 데이터셋 안내, enron.pst — 대용량 실전 검색 벤치마크 PST, korean_search_test.pst — 한글 비즈니스 메일 테스트셋, tools/benchmark.py — 검색 속도 재현, tools/pstpeek — PST CLI 검증 도구

## Knowledge Gaps
- **28 isolated node(s):** `form`, `addControls`, `settingsDialog`, `settingsForm`, `TAB_FILTERS` (+23 more)
  These have ≤1 connection - possible missing edges or undocumented components. (Counts symbols only; 100 node(s) total have ≤1 connection when file, concept and rationale nodes are included.)
- **1 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `AGENTS.md — 에이전트용 프로젝트 가이드` connect `PRD·문서 지식` to `외부 AI 전송`, `문서·첨부 텍스트 추출`, `검색 저장소·FTS`, `색인 파이프라인`, `근거 검색·답변 검증`, `웹 UI (app.js)`?**
  _High betweenness centrality (0.275) - this node is a cross-community bridge._
- **Why does `Integration` connect `통합 테스트` to `외부 AI 전송`, `로컬 Ollama 연결`, `검색 저장소·FTS`, `색인 파이프라인`?**
  _High betweenness centrality (0.154) - this node is a cross-community bridge._
- **Why does `Store` connect `검색 저장소·FTS` to `외부 AI 전송`, `통합 테스트`, `근거 검색·답변 검증`?**
  _High betweenness centrality (0.140) - this node is a cross-community bridge._
- **Are the 5 inferred relationships involving `Integration` (e.g. with `LocalAI` and `ExternalAI`) actually correct?**
  _`Integration` has 5 INFERRED edges - model-reasoned connections that need verification._
- **Are the 4 inferred relationships involving `ExternalAI` (e.g. with `Handler` and `main()`) actually correct?**
  _`ExternalAI` has 4 INFERRED edges - model-reasoned connections that need verification._
- **What connects `form`, `addControls`, `settingsDialog` to the rest of the system?**
  _28 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `외부 AI 전송` be split into smaller, more focused modules?**
  _Cohesion score 0.06753006475485661 - nodes in this community are weakly interconnected._