# 검색과 답변 성능 확인

## 1. 구간별 계측

Windows 기본 로그는 %LOCALAPPDATA%\OutlookMailSearch\app-runtime.log입니다. --data로 데이터 폴더를 지정한 경우 해당 폴더를 사용합니다.

```powershell
$searchLog = Join-Path $env:LOCALAPPDATA 'OutlookMailSearch\app-runtime.log'
Select-String -Path $searchLog -Pattern 'Search profile' | Select-Object -Last 30
```

로그의 같은 id로 검색과 원본 확인을 연결합니다. phase=query는 최초 검색, phase=verify는 원본 확인, phase=answer는 로컬 생성 요청입니다. fts_ms는 키워드 검색과 결과 미리보기, embed_ms는 검색어 벡터 요청, vector_ms는 후보 조회와 비교, rerank_ms는 재정렬, access_ms는 실제 파일·폴더 접근 확인입니다. 로컬 답변에는 context_ms, generation_ms, validation_ms도 기록합니다. 병렬 구간과 별도 요청이 있으므로 구간을 더한 값은 전체 시간과 다를 수 있습니다. 검색어와 메일 내용은 계측 로그에 기록하지 않습니다.

동일한 검색어·보관함 범위를 사용하고 처음 실행과 반복 실행을 구별해 비교하세요. 공유폴더의 첫 확인은 검색과 별도 요청으로 실행됩니다. 원본 보관함을 병렬로 검사하고, 확인된 결과를 먼저 돌려줍니다. 응답 기한을 넘긴 경로의 검사는 백그라운드에서 완료되면 캐시에 반영됩니다. 최근 확인이 성공한 결과는 최대 10초간 재사용합니다. 원문·첨부 열기와 답변 API 전송 전에는 접근을 다시 확인합니다. 기존 API 호출은 두 번째 요청 없이 접근 확인까지 마친 결과를 돌려줍니다. 브라우저는 defer_access를 지정해 분리된 경로를 씁니다.

## 2. 합성 데이터 전후 비교

업데이트본의 tools\benchmark.py 한 파일을 양쪽 소스에 대해 실행하면 동일한 데이터 생성 코드를 사용합니다. Windows PowerShell에서 아래 두 경로를 실제 소스 폴더로 바꾸세요.

```powershell
$benchBefore = 'C:\work\before\outlook-pst-search'
$benchAfter = 'C:\work\after\outlook-pst-search'
$benchScript = Join-Path $benchAfter 'tools\benchmark.py'
foreach ($benchSource in @($benchBefore, $benchAfter)) {
    $env:PYTHONPATH = $benchSource
    py -3 $benchScript --count 1500 --chunks 35 --repetitions 12
    py -3 $benchScript --semantic-scope --count 25000 --dimensions 1024 --repetitions 10
    py -3 $benchScript --duplicate-attachments --count 64
}
Remove-Item Env:\PYTHONPATH
```

- 본문은 Common Korean terms의 중앙값과 P95를 비교합니다. 위 크기는 약 57,000구간입니다. --count 13922로 늘리면 약 53만 구간이 되며 임시 디스크 공간 사용량도 늘어납니다.
- 벡터는 작은 보관함과 큰 보관함 필터의 반환 건수 및 P95를 봅니다. 이 시험은 검색어 벡터를 고정해 Ollama와 재정렬을 제외합니다.
- 중복 제거는 전체 구간과 임베딩에 실제로 보낸 텍스트 수를 봅니다. 변환·임베딩 시간을 실제 모델과 비교하려면 운영 로그에서 같은 버전에 대해 측정하세요.
- 브라우저에서 **의미 검색 결과 재정렬**을 껐다 켜며 같은 검색어를 두 번씩 실행합니다. 두 번째의 embed_ms가 쿼리 캐시 적중 시 0에 가까운지, 재정렬을 켰을 때 rerank_ms가 추가되는지 비교합니다.

## 3. 선택 사항인 USearch

사내에서 검증한 해당 Python·Windows 아키텍처의 USearch·NumPy 및 필요한 의존성 휠을 오프라인으로 설치한 뒤 아래처럼 실행합니다. 기본 실행은 SQLite를 사용합니다. NumPy를 사용할 수 있는 환경은 후보 벡터 계산을 배열 연산으로 처리합니다.

```powershell
py -3 -m pip install --no-index --find-links C:\approved-wheels usearch numpy
.\start-windows.cmd --vector-index usearch
py -3 -m tools.benchmark --semantic-scope --count 25000 --dimensions 1024 --repetitions 10 --vector-index usearch
```

첫 의미 검색을 시작하면 기존 SQLite 벡터에서 보관함·색인 버전·모델별 int8 HNSW 인덱스를 메모리에 만듭니다. 로그에 Vector shard ready가 나타난 뒤 같은 검색을 반복해 비교합니다. 앱 재시작 시 네이티브 인덱스는 다시 만들며 PST를 다시 추출하거나 기존 임베딩을 다시 계산할 필요는 없습니다. 보관함은 인덱스 선택으로 제한하고 날짜·발신자 등은 SQLite로 확인합니다. 후보를 더 가져와도 필터 결과 수가 부족하면 기존 SQLite 경로로 돌아갑니다. HNSW 후보의 최종 점수는 원래 float32 벡터로 계산합니다. 양자화 후 결과 재현율은 승인된 사내 질의로 별도 검증해야 합니다.

## 4. 회신과 인용 검증

메일은 인식한 답장 인용·서명 구간을 본문 색인에서 제외합니다. 원문 보기는 전체 본문을 사용합니다. 동일 첨부는 파일 종류와 바이트가 같은 경우에 성공한 추출과 본문 임베딩을 공유하며 메일별 원문 링크는 유지합니다. 기존 보관함에서 **갱신**을 실행하면 파일이 그대로인 PST도 구형 색인이면 새 형식으로 처리합니다. 저장해 둔 EML이 있으면 재사용합니다. 잠겼거나 다시 읽을 수 없는 PST는 이전 색인으로 검색할 수 있으며, 다음 갱신 때 다시 시도합니다.

로컬 답변은 관련 부모·자식 회신과 인접 문맥을 더 가져온 뒤 모델에 발송 시각순으로 제공합니다. 인용은 공백과 줄바꿈 차이를 허용하고 화면에는 원문 표기를 제공합니다. 복수 출처가 필요한 문장은 각 독립 사실에 근거가 있을 때 통과합니다. 실제 메일로는 최종 승인·취소 구별, 주체·수량 일치, 출처 링크 열기, 근거 부족 시 보류를 기준으로 수동 검증할 수 있습니다.

## 5. 질문 필터와 로컬 답변 용량

- 메일 질문의 **최근**은 오늘을 포함한 30일이며, **이번 주**는 월요일부터 일요일, **지난달**과 **올해**는 달력 기준입니다. 날짜는 PC의 현지 시각을 기준으로 비교합니다. 기간 두 개를 비교하거나 날짜가 납기·업무 일정으로 읽히는 표현은 자동 적용을 피합니다. UI에 날짜를 하나라도 입력했다면 선택한 날짜 범위를 사용합니다.
- `발신자: 홍길동`, `name@example.invalid이 보낸`, **첨부가 있는 메일**을 메일 검색 범위에 반영합니다. **첨부파일 내용 요약**은 첨부 본문에 우선순위를 줍니다. 명시한 UI의 발신자·첨부 조건이 우선합니다.
- 로컬 `/api/answer`는 근거 최대 18개와 원문 인용을 붙인 항목 최대 8개를 사용합니다. Ollama 컨텍스트는 기본 16,384토큰을 요청하며 모델의 최대값을 확인해 그 범위로 제한합니다. 모델 정보에 최대값이 없으면 8,192 이하를 사용합니다. 한도에 맞춰 후보를 일부 제외하면 로컬 모델이 반환한 답변을 부분 답변으로 표시합니다.

```powershell
.\start-windows.cmd --answer-num-ctx 8192
```

환경 변수 `MAILSEARCH_ANSWER_NUM_CTX`로도 2,048~32,768토큰 사이의 상한을 지정할 수 있습니다. GUI의 사내 API 답변은 검토한 발췌문을 전송하는 기존 입력 예산을 사용합니다. 앱을 시작하면 `app-runtime.log`의 `Vector search backend=` 줄에서 선택된 벡터 백엔드와 NumPy 사용 여부를 확인할 수 있습니다. USearch가 로드된 경우 개별 보관함의 HNSW 준비 상태는 `Vector shard ready` 로그로 확인합니다.

## 6. 백그라운드 임베딩과 벡터 실행 계획

색인 중 한 번에 처리할 구간 수는 24~64개, 요청 타임아웃은 30~60초 사이에서 지정합니다. 기본값은 24개와 60초입니다. 배치가 시간 초과되면 같은 위치에서 더 작은 배치로 재시도합니다. 검색어의 임베딩 제한은 별도로 적용됩니다.

```powershell
.\start-windows.cmd --embedding-batch-size 32 --embedding-batch-timeout 60
```

환경 변수는 `MAILSEARCH_EMBEDDING_BATCH_SIZE`와 `MAILSEARCH_EMBEDDING_BATCH_TIMEOUT`입니다.

SQLite에서 벡터 조회의 실행 계획을 확인하려면 아래를 실행합니다. `vectors`는 `(model, passage)` 기본 키를 사용합니다. 공유 벡터 조회에서 `embedding_refs`는 통계에 따라 `(model, passage)` 기본 키 또는 `embedding_ref_value` 커버링 인덱스를 사용하고, `embedding_values`는 `(model, digest)` 기본 키로 접근합니다.

```sql
EXPLAIN QUERY PLAN
SELECT passage, value
FROM searchable_vectors
WHERE model = 'bge-m3' AND passage IN (SELECT value FROM json_each('[1,2,3]'));
```
