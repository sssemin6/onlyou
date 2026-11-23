"use strict";

const element = (id) => document.getElementById(id);
const token = document.querySelector('meta[name="mail-session"]').content;
const form = element("search-form");
const sourcesList = element("sources");
const addForm = element("add-form");
const addOpen = element("add-open");
const sourcePath = element("source-path");
const sourceSelect = element("source");
const reviewBox = element("review");
const reviewSend = element("review-send");
const settingsDialog = element("settings-dialog");
const settingsForm = element("settings-form");
// 탭마다 쓸 수 있는 검색 범위가 다르다 — 문서엔 발신자가 없고 PCF는 전용 범위를 쓴다.
const TAB_FILTERS = {
  all: ["subject", "body", "after", "before"],
  mail: ["subject", "body", "sender", "recipient", "attachment", "after", "before"],
  document: ["subject", "body", "after", "before"],
  pcf: ["class", "flag", "owner", "hull", "drawing"],
};
const ALL_FILTERS = ["source", ...new Set(Object.values(TAB_FILTERS).flat())];
const dateFormat = new Intl.DateTimeFormat("ko-KR", { dateStyle: "medium", timeStyle: "short" });
const stateNames = {
  queued: "대기", extracting: "PST 읽는 중", headers: "자료 색인 중", attachments: "첨부 색인 중",
  embeddings: "의미 검색 준비 중", ready: "검색 가능", paused: "일시정지", error: "오류", empty: "가져온 자료 없음",
  removing: "삭제 중", remove_failed: "삭제 재시도 필요",
};
const issueNames = {
  readpst_missing: "PST 추출 도구(readpst)를 설치하거나 실행 경로를 지정하세요.",
  pst_in_use: "Outlook에서 사용 중이거나 접근할 수 없는 PST입니다. Outlook을 닫거나 사본을 등록하세요.",
  pst_export: "PST 추출에 실패했습니다. Outlook에서 해당 파일이 열리는지, 로컬 디스크에 여유 공간이 있는지 확인하세요.",
  pst_invalid: "Outlook PST 헤더를 읽을 수 없습니다. Outlook에서 만든 .pst 파일인지 확인하세요.",
  pst_no_mail: "추출된 메일이 없습니다. Outlook에서 이 PST를 열어 받은 편지함이나 하위 폴더에 메일이 있는지 확인하세요.",
  eml_no_mail: "읽을 수 있는 PST·EML 또는 지원 문서가 있는지 확인하세요.",
  document_extract_failed: "문서 내용을 가져오지 못했습니다. 아래 누락 사유를 확인하고 다시 색인하세요.",
  mail_parse_failed: "가져온 메일을 읽을 수 없습니다. 아래 실패 건수를 확인하고 다시 색인해 보세요.",
  readpst_dependencies: "readpst가 시작되지 않았습니다. MSYS2에서 함께 설치된 DLL이 있는지 확인하세요.",
  disk_or_source: "파일 접근 권한과 저장 공간을 확인하세요.",
  changed: "파일이 변경되었습니다. 갱신해 주세요.",
  interrupted: "중단된 색인입니다.",
  embedding_unavailable: "의미 색인을 완료하지 못했습니다. 키워드 검색은 사용 가능하며, 연결을 확인한 뒤 보관함을 갱신할 수 있습니다.",
  pcf_input_too_large: "선택한 COMMENT이 현재 입력 한도를 초과합니다.",
  gateway_indexing: "색인 중에는 모델 설정을 유지합니다. 색인이 끝나거나 일시정지한 뒤 적용하세요.",
  gateway_models: "용도별 모델 선택을 확인하세요.",
  gateway_url: "설정 창에서 올바른 사내 HTTPS 주소를 입력하세요.",
  gateway_settings: "사내 API 주소와 인증서 경로를 확인하세요.",
  gateway_preset_missing: "이 배포본에 사내 기본값이 없습니다. 주소와 모델 ID를 직접 입력하거나 사내용 배포본을 실행하세요.",
  gateway_preset_invalid: "프리셋의 HTTPS 주소, 모델 ID와 인증서 설정을 확인하세요.",
  gateway_preset_io: "앱 폴더의 gateway-defaults.json 파일을 읽거나 쓸 수 없습니다.",
  api_settings_save_failed: "설정을 저장하지 못했습니다. 앱 데이터 폴더에 쓸 수 있는지 확인하세요.",
  embedding_api_key: "사내 API 키에 공백이 있는지 확인하세요.",
  external_disabled: "상단의 API 설정에서 답변에 사용할 AI를 연결해 주세요.",
  external_url: "HTTPS 주소를 확인하세요. 주소는 /chat/completions 또는 /messages로 끝나야 합니다.",
  external_model: "제공자에서 안내한 모델명을 입력하세요.",
  external_key: "API 키에 공백이 있는지 확인하세요.",
  external_key_required: "이 주소에서 사용할 API 키를 입력하세요.",
  external_test_auth: "인증이 거부되었습니다. API 키와 모델 사용 권한을 확인하세요.",
  external_test_endpoint: "요청이 거부되었습니다. 주소, 모델명, JSON 모드 지원 여부를 확인하세요.",
  external_test_limit: "제공자의 요청 한도 또는 잔액을 확인하세요.",
  external_test_failed: "연결 테스트에 실패했습니다. 네트워크와 제공자의 응답을 확인하세요.",
  external_busy: "외부 API가 다른 질문을 처리하고 있습니다. 잠시 후 다시 시도하세요.",
  external_review_expired: "전송 검토가 만료되었습니다. 질문을 다시 실행해 주세요.",
  external_source_changed: "검토한 자료가 변경되었거나 원본에 접근할 수 없습니다. 질문을 다시 실행해 주세요.",
  source_path: "경로를 열 수 없습니다. 공유 폴더라면 파일 탐색기에서 연결 상태와 경로를 확인하세요.",
  source_access: "파일이나 폴더를 읽을 권한이 없습니다. Windows 계정의 공유 및 파일 권한을 확인하세요.",
  source_format: "PST, EML·문서 파일(PDF·DOCX·XLSX·TXT) 또는 그런 파일이 든 폴더를 등록할 수 있습니다.",
  source_busy: "현재 작업이 끝난 뒤 다시 시도하세요.",
  source_missing: "보관함을 찾지 못했습니다.",
  evidence_missing: "이 근거의 보관함이 갱신되었거나 접근 권한이 사라졌습니다.",
  invalid_input: "검색 범위와 입력 값을 확인하세요.",
  question_required: "질문을 입력하세요.",
  index_unavailable: "색인 DB에 접근하지 못했습니다. 잠시 후 다시 시도하세요.",
};
let previousQuestion = "";
let request;
let pendingReview;
let sourceSnapshot = "";
let sourceRequest = 0;
let settingsRequest = 0;

const TABS = [["tab-all", "all"], ["tab-mail", "mail"], ["tab-doc", "document"], ["tab-pcf", "pcf"]];
const SEARCH_HINT = {
  all: "예: A17 변경 승인 메일과 양산 BOM",
  mail: "예: A-17 최종 납품일",
  document: "예: A17 양산 BOM Rev.C 또는 허용 공차",
  pcf: "선주 COMMENT나 호선·도면번호로 검색",
};
let activeTab = "mail";
function selectTab(value) {
  if (value !== activeTab) {
    clearSearch();
    setQuestionContext("");
  }
  activeTab = value;
  for (const [id, tab] of TABS) {
    element(id).classList.toggle("active", value === tab);
    element(id).setAttribute("aria-selected", String(value === tab));
    element(id).tabIndex = value === tab ? 0 : -1;
  }
  document.querySelectorAll(".filter-grid label[data-show]").forEach((field) => {
    field.hidden = !field.dataset.show.split(" ").includes(value);
  });
  element("query").placeholder = SEARCH_HINT[value];
  element("pcf-scope-help").hidden = value !== "pcf";
  showFilterCount();
  try { localStorage.setItem("mailsearch-tab", value); } catch { /* private mode */ }
}
for (const [id, value] of TABS) {
  const tab = element(id);
  tab.addEventListener("click", () => selectTab(value));
  tab.addEventListener("keydown", (event) => {
    const index = TABS.findIndex(([key]) => key === id);
    const offset = { ArrowLeft: -1, ArrowRight: 1 }[event.key];
    if (!offset && !["Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const next = event.key === "Home" ? 0 : event.key === "End" ? TABS.length - 1
      : (index + offset + TABS.length) % TABS.length;
    selectTab(TABS[next][1]);
    element(TABS[next][0]).focus();
  });
}
let savedTab;
try { savedTab = localStorage.getItem("mailsearch-tab"); } catch { /* private mode */ }
selectTab(Object.hasOwn(SEARCH_HINT, savedTab) ? savedTab : "all");

function node(tag, text, className) {
  const result = document.createElement(tag);
  if (text) result.textContent = text;
  if (className) result.className = className;
  return result;
}

async function call(route, data, signal) {
  const headers = { "X-Mail-Session": token };
  const options = { headers, signal, cache: "no-store" };
  if (data !== undefined) {
    options.method = "POST";
    headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(data);
  }
  const response = await fetch(route, options);
  const body = await response.json();
  if (!response.ok) throw new Error(issueNames[body.error] || "이 PC의 검색 앱과 연결하지 못했습니다.");
  return body;
}

function notice(message) {
  element("notice").textContent = message;
}

function setQuestionContext(value) {
  previousQuestion = value;
  element("context").hidden = !value;
  element("context-text").textContent = value ? "이전 질문 · " + value : "";
}

function formatDate(value) {
  const date = value ? new Date(value) : null;
  return date && !Number.isNaN(date.getTime()) ? dateFormat.format(date) : "날짜 미상";
}

function showFilterCount() {
  const count = Number(Boolean(sourceSelect.value)) + TAB_FILTERS[activeTab].filter((id) => element(id).value.trim()).length;
  const badge = element("filter-count");
  badge.textContent = count;
  badge.hidden = !count;
  element("clear-filters").hidden = !count;
}

function kindCountLine(source) {
  const counts = source.counts || {};
  const parts = [["mail", "메일"], ["document", "문서"], ["pcf", "PCF"]]
    .filter(([kind]) => counts[kind])
    .map(([kind, label]) => label + " " + counts[kind].toLocaleString("ko-KR") + "건");
  return parts.length ? parts.join(" · ") : "항목 " + source.done.toLocaleString("ko-KR") + "건";
}

async function showSources() {
  const version = ++sourceRequest;
  const data = await call("/api/sources");
  if (version !== sourceRequest) return;
  const next = JSON.stringify(data);
  if (next === sourceSnapshot) return;
  sourceSnapshot = next;
  const focusedAction = sourcesList.contains(document.activeElement) ? document.activeElement.dataset.action : "";
  sourcesList.replaceChildren();
  if (!data.sources.length) {
    sourcesList.append(node("p", "등록된 자료가 없습니다.", "source-empty"));
  }
  const selectedSource = sourceSelect.value;
  sourceSelect.replaceChildren(new Option("전체 보관함", ""));
  for (const source of data.sources) {
    sourceSelect.append(new Option(source.path, source.id));
    const name = source.path.split(/[\\/]/).pop();
    const entry = node("div", "", "source-card");
    entry.dataset.state = source.state;
    const heading = node("h3", name, "source-name");
    heading.title = source.path;
    entry.append(heading, node("p", (stateNames[source.state] || source.state) + " · " + kindCountLine(source), "source-state"));
    if (source.failed || source.omitted || source.partial) {
      entry.append(node("p", "실패 " + source.failed + " · 누락 " + source.omitted + " · 일부 색인 " + source.partial));
      for (const problem of source.problems || []) {
        entry.append(node("p", problem.item + " · " + (issueNames[problem.code] || problem.code), "issue"));
      }
    }
    if (source.updated) {
      const existing = source.active !== null && source.pending !== null;
      entry.append(node("p", (existing ? "이전 색인 사용 중 · " : "색인 기준 ") + formatDate(source.updated)));
    }
    if (source.issue) entry.append(node("p", issueNames[source.issue] || source.issue, "issue"));
    if (source.updates?.length && ["ready", "empty", "error"].includes(source.state)) {
      entry.append(node("p", "갱신 대상 PST " + source.updates.length + "개", "issue"));
    }
    if (!source.available && source.state !== "removing") entry.append(node("p", "원본 경로 접근 불가", "issue"));
    const actions = node("div", "", "source-actions");
    const choices = ["queued", "extracting", "headers", "attachments", "embeddings"].includes(source.state)
      ? [["pause", "일시정지"]]
      : source.state === "ready" ? [["refresh", source.partial || source.omitted ? "다시 색인" : "갱신"]]
      : source.state === "removing" || source.state === "remove_failed" ? []
      : source.pending === null || ["pst_no_mail", "eml_no_mail", "mail_parse_failed", "document_extract_failed"].includes(source.issue)
        ? [["refresh", "다시 색인"]] : [["resume", "재개"], ["refresh", "다시 색인"]];
    if (source.state !== "removing") choices.push(["remove", source.state === "remove_failed" ? "삭제 재시도" : "등록 해제"]);
    for (const [action, label] of choices) {
      const button = node("button", label, "text-button");
      button.type = "button";
      button.dataset.action = action + "-" + source.id;
      button.addEventListener("click", async () => {
        if (action === "remove" && !confirm(name + "의 색인과 임시 추출물을 삭제할까요?")) return;
        button.disabled = true;
        try {
          await call("/api/source-action", { source: source.id, action });
          if (action === "remove") {
            clearSearch();
            if (element("viewer").open) element("viewer").close();
            notice("보관함을 등록 해제했습니다.");
          }
          await showSources();
        } catch (error) {
          notice(error.message);
        } finally {
          button.disabled = false;
        }
      });
      actions.append(button);
    }
    entry.append(actions);
    sourcesList.append(entry);
  }
  sourceSelect.value = selectedSource;
  if (sourceSelect.selectedIndex < 0) {
    sourceSelect.value = "";
    clearSearch();
  }
  showFilterCount();
  if (focusedAction) {
    const replacement = [...sourcesList.querySelectorAll("button")]
      .find((button) => button.dataset.action === focusedAction);
    (replacement || addOpen).focus({ preventScroll: true });
  }
  const modelStatus = element("model-status");
  modelStatus.parentElement.dataset.configured = Boolean(data.embedding);
  if (data.embedding_endpoint) {
    modelStatus.textContent = "사내 API 의미 검색 · " + data.embedding;
    modelStatus.title = data.embedding_endpoint;
  } else {
    modelStatus.textContent = data.embedding ? "로컬 의미 검색 · " + data.embedding : "로컬 키워드 검색";
    modelStatus.removeAttribute("title");
  }
  element("external-panel").dataset.configured = Boolean(data.external);
  const externalStatus = element("external-status");
  externalStatus.textContent = data.external
    ? "답변 API · " + new URL(data.external.destination).host + " · " + data.external.model : "답변 API 연결 전";
  externalStatus.title = data.external?.destination || "";
}

function highlight(text, quote) {
  const container = node("pre");
  const start = quote ? text.indexOf(quote) : -1;
  if (start < 0) {
    container.textContent = text;
  } else {
    container.append(text.slice(0, start), node("mark", quote), text.slice(start + quote.length));
  }
  return container;
}

let viewerRequest = 0;
async function openEvidence(id, quote) {
  const version = ++viewerRequest;
  const dialog = element("viewer");
  element("viewer-title").textContent = "원문";
  const body = element("viewer-content");
  body.replaceChildren(node("p", "원문을 여는 중입니다."));
  if (!dialog.open) dialog.showModal();
  try {
    const item = await call("/api/evidence/" + id);
    if (version !== viewerRequest || !dialog.open) return;
    element("viewer-title").textContent = item.subject || "제목 없는 메일";
    dialog.scrollTop = 0;
    const metadata = node("dl", "", "viewer-meta");
    const mail = item.kind === "mail";
    const fields = mail ? [
      ["발신자", item.sender || "정보 없음"],
      ["수신자", item.recipient || "정보 없음"],
      ["발송일", formatDate(item.sent)],
      ["출처", item.archive + " · " + item.folder],
    ] : [
      ["파일", item.subject],
      ["파일 수정일", formatDate(item.sent)],
      ["보관함", item.archive + " · " + item.folder],
      ["위치", item.location],
    ];
    if (mail && item.attachment) fields.push(["첨부", item.attachment + " · " + item.location]);
    if (!mail && item.attachments[0]) fields.push(["추출 상태", item.attachments[0].status]);
    for (const [label, value] of fields) metadata.append(node("dt", label), node("dd", value));
    body.replaceChildren(metadata);
    if (quote) body.append(node("blockquote", quote));
    if (item.pcf) {
      body.append(node("h3", "COMMENT " + item.pcf.number + (item.pcf.revision ? " · Rev. " + item.pcf.revision : "")));
      for (const warning of item.pcf.warnings) body.append(node("p", warning, "answer-status"));
      for (const cell of item.pcf.cells) {
        const heading = cell.location + (cell.label ? " · " + cell.label : "");
        body.append(node("h4", heading));
        const original = highlight(cell.text || "원본 셀 빈칸", cell.passage === id ? quote : null);
        body.append(original);
        if (cell.passage === id) requestAnimationFrame(() => original.scrollIntoView({ block: "nearest" }));
      }
    } else {
      const text = item.attachment ? item.text : item.body || item.text;
      body.append(highlight(text, quote || item.text));
    }
    if (mail && item.attachments.length) {
      const attachments = node("ul");
      body.append(node("h3", "첨부"), attachments);
      for (const attachment of item.attachments) {
        const row = node("li");
        if (attachment.passage) {
          const link = node("button", attachment.name, "text-button");
          link.type = "button";
          link.addEventListener("click", () => openEvidence(attachment.passage));
          row.append(link, " · " + attachment.status);
        } else row.textContent = attachment.name + " · " + attachment.status;
        attachments.append(row);
      }
    }
  } catch (error) {
    if (version === viewerRequest && dialog.open) body.replaceChildren(node("p", error.message));
  }
}

function recentTitle() {
  if (activeTab === "pcf") return "최근 PCF 문서";
  if (activeTab === "document") return "최근 문서";
  return activeTab === "mail" ? "최근 메일" : "최근 자료";
}

function marked(text, words) {
  // Split text into plain and <mark> nodes around every matched word.
  const parts = [];
  const lower = text.toLowerCase();
  let index = 0;
  while (index < text.length) {
    let at = -1;
    let hit = "";
    for (const word of words) {
      const found = lower.indexOf(word.toLowerCase(), index);
      if (found >= 0 && (at < 0 || found < at)) { at = found; hit = word; }
    }
    if (at < 0) break;
    parts.push(text.slice(index, at), node("mark", text.slice(at, at + hit.length)));
    index = at + hit.length;
  }
  parts.push(text.slice(index));
  return parts;
}

function showResults(rows, title) {
  const result = element("results");
  result.replaceChildren();
  element("result-section").hidden = false;
  element("result-heading").textContent = title + " (" + rows.length + ")";
  if (!rows.length) result.append(node("p", "이 범위에서 일치하는 자료를 찾지 못했습니다.", "result-empty"));
  for (const row of rows) {
    const button = node("button", "", "result");
    button.type = "button";
    const reasons = [];
    if (row.matched?.length) reasons.push("단어 " + row.matched.slice(0, 3).join(" · "));
    if (row.similarity != null) reasons.push("유사도 " + Math.round(row.similarity * 100) + "%");
    button.append(node("strong", row.subject || "제목 없는 메일"),
      node("span", [row.sender, formatDate(row.sent), row.archive, row.folder, row.kind === "mail" ? row.attachment : null, row.attachment || row.kind !== "mail" ? row.location : ""].filter(Boolean).join(" · "), "result-meta"));
    if (reasons.length) button.append(node("span", reasons.join(" · "), "result-why"));
    const preview = node("span", "", "result-preview");
    if (row.snippet && row.matched?.length) preview.append(...marked(row.snippet, row.matched));
    else preview.textContent = row.preview;
    button.append(preview);
    button.addEventListener("click", () => openEvidence(row.passage));
    result.append(button);
  }
}

function showAnswer(data) {
  const box = element("answer");
  const statuses = {
    answered: "선택한 근거로 찾은 답변", partial: "일부 근거만 확인했습니다",
    ambiguous: data.claims.length ? "근거에 서로 다른 내용이 있습니다. 각 원문을 확인해 주세요." : "여러 건이 해당합니다. 품번이나 기간을 질문에 추가해 주세요.",
    insufficient: "검색된 자료에서 답을 확인하지 못했습니다.",
    model_busy: "로컬 AI가 다른 질문을 처리 중입니다. 잠시 후 다시 시도하세요.",
    model_not_configured: "로컬 AI 모델을 연결하면 근거를 바탕으로 답변할 수 있습니다.",
    model_unavailable: "로컬 AI 모델을 열지 못했습니다. 아래 메일을 직접 확인할 수 있습니다.",
    external_unavailable: "외부 API 응답을 받지 못했습니다. 연결 주소, 키, 모델, 네트워크를 확인하세요.",
  };
  const heading = node("h2", "AI 답변");
  box.replaceChildren(heading, node("p", statuses[data.status] || "", "answer-status"));
  let count = 0;
  for (const claim of data.claims) {
    const paragraph = node("p", claim.text + " ", "answer-claim");
    for (const ref of claim.citations) {
      const cite = node("button", "[" + ++count + "]", "cite");
      cite.type = "button";
      cite.title = ref.quote;
      cite.setAttribute("aria-label", "근거 " + count + " · " + ref.subject);
      cite.addEventListener("click", () => openEvidence(ref.passage, ref.quote));
      paragraph.append(cite);
    }
    box.append(paragraph);
  }
  if (data.pcf_coverage) {
    const coverage = data.pcf_coverage;
    box.append(node("p", "전체 " + coverage.expected + "셀 중 " + coverage.included + "셀 처리", "answer-status"));
    if (coverage.split && !coverage.synthesized) box.append(node("p", "나누어 처리한 구간별 답변입니다.", "answer-status"));
    if (coverage.failed) box.append(node("p", "일부 API 호출을 완료하지 못했습니다.", "answer-status"));
    for (const warning of coverage.warnings || []) box.append(node("p", warning, "answer-status"));
    for (const unknown of data.unknowns || []) box.append(node("p", "확인 필요 · " + unknown, "answer-status"));
  }
  box.hidden = false;
}

function rememberQuestion(data, query) {
  if (!["answered", "partial"].includes(data.status)) return;
  const followUp = /^(그|그리고|뒤에|누가|언제|어떻게|왜)/.test(query);
  setQuestionContext((followUp && previousQuestion ? previousQuestion + " / " + query : query).slice(-450));
}

function clearReview() {
  pendingReview = null;
  reviewBox.hidden = true;
  element("review-excerpts").replaceChildren();
}

function clearSearch() {
  request?.abort();
  clearReview();
  element("answer").hidden = true;
  element("result-section").hidden = true;
  notice("");
}

function showReview(data, pcf) {
  pendingReview = { ...data, pcf };
  element("review-destination").textContent = data.model + " · " + data.destination;
  element("review-question").textContent = data.question;
  const excerpts = element("review-excerpts");
  excerpts.replaceChildren();
  if (data.pcf_coverage) {
    const coverage = data.pcf_coverage;
    excerpts.append(node("p", "전체 " + coverage.expected + "셀 중 " + coverage.included +
      "셀 선택 가능 · 전체 선택 시 최대 " + coverage.calls + "회 호출", "answer-status"));
    if (coverage.revision) excerpts.append(node("p", "Rev. " + coverage.revision, "answer-status"));
    for (const warning of coverage.warnings) excerpts.append(node("p", warning, "answer-status"));
  }
  for (const excerpt of data.excerpts) {
    const card = node("div", "", "review-excerpt");
    const label = node("label", "", "review-choice");
    const checkbox = node("input");
    checkbox.type = "checkbox";
    checkbox.value = excerpt.id;
    checkbox.checked = true;
    checkbox.addEventListener("change", () => {
      reviewSend.disabled = !excerpts.querySelector("input:checked");
    });
    const title = excerpt.source;
    label.append(checkbox, node("span", title.subject + " · " + title.location));
    const original = node("button", "원문 보기", "text-button");
    original.type = "button";
    original.addEventListener("click", () => openEvidence(title.passage));
    card.append(label, original);
    if (excerpt.context) {
      const { subject, sender, sent, attachment, location } = excerpt.context;
      card.append(node("p", "함께 전송 · " + [subject, sender, sent, attachment, location].filter(Boolean).join(" · ")));
    }
    card.append(node("p", excerpt.text || "원본 셀 빈칸"));
    excerpts.append(card);
  }
  reviewSend.disabled = false;
  reviewBox.hidden = false;
  reviewBox.scrollIntoView({ block: "nearest" });
}

async function previewAnswer(route, payload, active, pcf) {
  const data = await call(route, payload, active.signal);
  if (active.signal.aborted) return;
  if (data.review) {
    notice("");
    element("result-section").hidden = true;
    showReview(data, pcf);
    return;
  }
  showResults(data.results, "근거 후보");
  notice(data.pcf_issue || "답변에 보낼 발췌문을 찾지 못했습니다.");
  if (!data.choose_case) return;
  const choices = element("results").querySelectorAll(".result");
  data.results.forEach((result, index) => {
    const button = node("button", "이 COMMENT으로 답변", "text-button");
    button.type = "button";
    choices[index].after(button);
    button.addEventListener("click", async () => {
      request?.abort();
      const next = request = new AbortController();
      try {
        await previewAnswer(route, { ...payload, passage: result.passage }, next, pcf);
      } catch (error) {
        if (!next.signal.aborted) notice(error.message);
      }
    });
  });
}

element("query").addEventListener("input", clearSearch);
form.addEventListener("submit", async (event) => {
  event.preventDefault();
  clearSearch();
  const active = request = new AbortController();
  const query = element("query").value.trim();
  const ask = event.submitter?.value === "ask";
  const pcf = activeTab === "pcf";
  if (ask && !query) {
    element("query").focus();
    notice("질문을 입력하세요.");
    return;
  }
  const filters = Object.fromEntries(["source", ...TAB_FILTERS[activeTab]].map((id) => [id, element(id).value]));
  filters.tab = activeTab;
  const payload = { query, filters, context: ask ? previousQuestion : "" };
  notice(ask ? "답변에 쓸 근거를 찾고 있습니다." : "검색 중입니다.");
  try {
    if (ask) {
      await previewAnswer(pcf ? "/api/pcf/preview" : "/api/external/preview", payload, active, pcf);
    } else {
      const data = await call("/api/search", payload, active.signal);
      if (active.signal.aborted) return;
      showResults(data.results, query ? "검색 결과" : recentTitle());
      notice("");
    }
  } catch (error) {
    if (!active.signal.aborted) notice(error.message);
  }
});

reviewSend.addEventListener("click", async () => {
  const review = pendingReview;
  const selected = [...element("review-excerpts").querySelectorAll("input:checked")].map((input) => Number(input.value));
  if (!review || !selected.length) return;
  request?.abort();
  const active = request = new AbortController();
  clearReview();
  notice("답변 API 응답을 기다리는 중입니다.");
  try {
    const data = await call(review.pcf ? "/api/pcf/send" : "/api/external/send",
      { review: review.review, excerpts: selected }, active.signal);
    if (active.signal.aborted) return;
    showAnswer(data);
    if (data.status !== "external_unavailable") showResults(data.results, "사용한 근거");
    rememberQuestion(data, review.question);
    notice("");
  } catch (error) {
    if (!active.signal.aborted) notice(error.message);
  }
});
element("review-cancel").addEventListener("click", () => {
  clearReview();
  element("submit-ask").focus();
});

element("clear-context").addEventListener("click", () => {
  setQuestionContext("");
  clearSearch();
  element("query").focus();
});
function scopeChanged() {
  clearSearch();
  setQuestionContext("");
  showFilterCount();
}
for (const id of ALL_FILTERS) element(id).addEventListener("input", scopeChanged);
element("clear-filters").addEventListener("click", () => {
  for (const id of ["source", ...TAB_FILTERS[activeTab]]) element(id).value = "";
  scopeChanged();
  element("query").focus();
});
function settingsMessage(message) {
  element("settings-status").textContent = message;
}

const gatewayRoles = {
  answer: ["답변 모델", "사용 안 함"], embedding: ["임베딩 실행 위치", "임베딩 사용 안 함"],
  rerank: ["검색 후보 재정렬", "사용 안 함"],
};
const gatewayCheckReasons = {
  tls_certificate: "TLS 인증서를 신뢰할 수 없습니다. 고급 설정의 CA 인증서 경로를 확인하세요.",
  missing_ca: "고급 설정의 CA 인증서 파일을 찾을 수 없습니다.",
  tls_error: "TLS 연결 또는 CA 파일에 오류가 있습니다. 연결 로그를 확인하세요.",
  dns: "사내 API 주소의 이름을 확인할 수 없습니다.",
  timeout: "서버 응답 시간이 초과되었습니다.",
  connection: "사내 API 서버에 연결하지 못했습니다.",
  invalid_response: "서버 응답 형식이 예상과 다릅니다.",
  ollama_not_found: "이 PC에서 Ollama를 찾을 수 없습니다.",
  ollama_not_started: "로컬 Ollama 서버를 시작하지 못했습니다.",
  local_model_missing: "Ollama에 로컬 bge-m3 모델이 없습니다.",
  local_model_http: "Ollama가 오류를 반환했습니다.",
  local_connection: "로컬 Ollama에 연결하지 못했습니다.",
};
const gatewayHttpReasons = {
  400: "서버가 모델 또는 요청 형식을 거부했습니다.",
  401: "API 키 인증이 실패했습니다.",
  403: "API 키의 모델 사용 권한을 확인하세요.",
  404: "모델 ID와 API 경로를 확인하세요.",
  429: "요청 한도를 초과했습니다.",
};
const localEmbedding = "ollama:bge-m3";
let gatewaySettings;
let genericSettings;
let lastSettingsProvider;
const settingsDrafts = {};
const settingsProvider = element("settings-provider");
const gatewayAdvanced = element("gateway-advanced");
const ollamaPanel = element("ollama-setup");
let ollamaRequest = 0;

const ollamaPhases = {
  checking: "로컬 Ollama 상태를 확인하고 필요하면 실행합니다.",
  installing: "Ollama를 설치하는 중입니다.",
  downloading: "BGE-M3를 내려받는 중입니다.",
};
const ollamaErrors = {
  ollama_not_started: "Ollama를 시작하지 못했습니다. 실행 상태를 확인하고 다시 시도하세요.",
  ollama_install_failed: "Ollama 설치에 실패했습니다. 앱 데이터 폴더의 api-connection.log에서 원인을 확인하세요.",
  ollama_model_check_failed: "BGE-M3 보유 여부를 확인하지 못했습니다. Ollama 상태를 확인하세요.",
  ollama_model_download_failed: "BGE-M3 다운로드에 실패했습니다. 앱 데이터 폴더의 api-connection.log를 확인하세요.",
};

function renderOllamaSetup(state) {
  const busy = state.phase !== "idle";
  const status = (value, yes, no) => value === null ? "미확인" : value ? yes : no;
  const parts = [
    "Ollama " + status(state.installed, "설치됨", "미설치"),
    status(state.running, "실행 중", "미실행"),
    "BGE-M3 " + status(state.model_present, "보유", "미보유"),
  ];
  if (state.error) parts.push(ollamaErrors[state.error]);
  else if (state.installed === false && !state.can_install) parts.push("자동 설치에는 Windows와 WinGet 또는 시스템 curl.exe가 필요합니다.");
  const percent = Number.isInteger(state.progress) ? " " + state.progress + "%" : "";
  element("ollama-setup-status").textContent = busy ? ollamaPhases[state.phase] + percent : parts.join(" · ");
  const progress = element("ollama-setup-progress");
  progress.hidden = state.phase !== "installing" && state.phase !== "downloading";
  if (percent) progress.value = state.progress;
  else progress.removeAttribute("value");
  element("ollama-install").hidden = busy || !state.can_install;
  element("ollama-install").textContent = state.installed ? "BGE-M3 다운로드" : "Ollama와 BGE-M3 설치";
  element("ollama-install-help").hidden = busy || !state.can_install;
  element("ollama-check").hidden = busy || state.model_present === true;
}

async function updateOllamaSetup(action) {
  const version = ++ollamaRequest;
  const current = () => version === ollamaRequest && settingsDialog.open && !ollamaPanel.hidden;
  const endpoint = "/api/ollama/setup";
  renderOllamaSetup({ phase: "checking" });
  try {
    let state = await call(endpoint, { action }, AbortSignal.timeout(10000));
    while (current()) {
      renderOllamaSetup(state);
      if (state.phase === "idle") return;
      await new Promise(resolve => setTimeout(resolve, 1000));
      if (!current()) return;
      state = await call(endpoint, undefined, AbortSignal.timeout(10000));
    }
  } catch {
    if (current()) {
      element("ollama-setup-status").textContent = "로컬 Ollama 상태를 불러오지 못했습니다.";
      element("ollama-check").hidden = false;
    }
  }
}

function showOllamaSetup() {
  ++ollamaRequest;
  ollamaPanel.hidden = settingsProvider.value !== "intranet" || element("gateway-embedding")?.value !== localEmbedding;
  if (!ollamaPanel.hidden) updateOllamaSetup("check");
}

element("ollama-install").addEventListener("click", () => updateOllamaSetup("install"));
element("ollama-check").addEventListener("click", () => updateOllamaSetup("check"));

function setSettingsBusy(busy) {
  const inactive = settingsProvider.value === "intranet" ? "settings-generic" : "settings-gateway";
  for (const control of settingsForm.querySelectorAll("input, button, select")) {
    if (control.id !== "settings-close") control.disabled = busy || element(inactive).contains(control);
  }
}

function setGatewayAnswers(selected) {
  const answer = element("gateway-answer");
  const names = new Set(element("gateway-answer-models").value.split(",").map(value => value.trim()).filter(Boolean));
  answer.replaceChildren(new Option("사용 안 함", ""));
  for (const name of names) answer.add(new Option(name, name));
  answer.value = names.has(selected) ? selected : "";
}

element("gateway-answer-models").addEventListener("input", () => {
  const answer = element("gateway-answer");
  if (answer) setGatewayAnswers(answer.value);
});

async function applyGatewayDefaults() {
  const version = settingsRequest;
  const current = () => version === settingsRequest && settingsDialog.open;
  setSettingsBusy(true);
  try {
    const preset = await call("/api/intranet/defaults", {}, AbortSignal.timeout(10000));
    if (!current()) return;
    const fields = readSettingsFields(true);
    const sameAddress = fields.gateway_url === preset.gateway_url;
    const models = preset.answer_models.split(",");
    fields.selections.answer = models.includes(fields.selections.answer) ? fields.selections.answer : models[0];
    settingsDrafts.intranet = {
      ...fields, ...preset, key: sameAddress ? fields.key : "",
      ca_file: preset.ca_file || (sameAddress ? fields.ca_file : ""),
    };
    renderSettings();
    settingsMessage("기본값을 불러왔습니다. 연결에 반영하려면 아래 적용 버튼을 누르세요.");
  } catch (error) {
    if (current()) settingsMessage(error.message);
  } finally {
    if (current()) setSettingsBusy(false);
  }
}

element("gateway-defaults").addEventListener("click", applyGatewayDefaults);

function readSettingsFields(gateway) {
  const key = element("settings-key").value;
  if (!gateway) {
    return { key, destination: element("settings-url").value, model: element("settings-model").value };
  }
  return {
    key, gateway_url: element("gateway-url").value,
    answer_models: element("gateway-answer-models").value, ca_file: element("gateway-ca-file").value,
    test_unverified: element("gateway-test-unverified").checked,
    selections: Object.fromEntries(Object.keys(gatewayRoles).map(role => [role, element("gateway-" + role).value])),
  };
}

settingsForm.addEventListener("input", (event) => {
  if (event.target !== settingsProvider) {
    if (event.target.id === "gateway-url") element("gateway-test-unverified").checked = false;
    const provider = settingsProvider.value;
    settingsDrafts[provider] = readSettingsFields(provider === "intranet");
  }
});
settingsForm.addEventListener("invalid", (event) => {
  if (gatewayAdvanced.contains(event.target)) gatewayAdvanced.open = true;
}, true);

function renderSettings() {
  const provider = settingsProvider.value;
  const gateway = provider === "intranet";
  const draft = settingsDrafts[provider];
  element("settings-generic").hidden = gateway;
  element("settings-gateway").hidden = !gateway;
  element("settings-url").required = !gateway;
  element("settings-model").required = !gateway;
  element("settings-key").value = draft?.key ?? "";
  const settings = gateway ? gatewaySettings : genericSettings;
  element("settings-key-help").textContent = settings?.persistent_keys
    ? "적용하면 키를 현재 Windows 사용자 계정으로 암호화해 저장합니다. 연결 해제로 삭제할 수 있습니다."
    : "이 환경에서는 키를 앱이 실행되는 동안 사용합니다.";
  if (gateway) {
    if (!gatewaySettings) return;
    const fields = draft ?? gatewaySettings;
    element("gateway-url").value = fields.gateway_url;
    element("gateway-answer-models").value = fields.answer_models;
    element("gateway-ca-file").value = fields.ca_file;
    element("gateway-test-unverified").checked = fields.test_unverified ?? false;
    element("settings-current").textContent = gatewaySettings.configured
      ? (gatewaySettings.active_answer ? "사내 답변 모델 사용 중" : "사내 API 키 등록됨")
      : (fields.gateway_url ? "사내 API를 선택한 기능에만 키가 필요합니다."
        : "사내 API를 사용하려면 고급 설정에 HTTPS 주소를 입력하세요.");
    element("settings-remove").hidden = !gatewaySettings.configured;
    element("settings-key").placeholder = gatewaySettings.configured ? "등록된 사내 키를 쓰려면 비워 두세요" : "";
    const roles = element("settings-roles");
    roles.replaceChildren();
    for (const [role, [label, disabled]] of Object.entries(gatewayRoles)) {
      const field = node("select");
      field.id = "gateway-" + role;
      const title = node("label", label);
      title.htmlFor = field.id;
      field.add(new Option(disabled, ""));
      for (const [id, name] of Object.entries(gatewaySettings.models[role])) {
        field.add(new Option(name, id));
      }
      field.value = fields.selections[role];
      roles.append(title, field);
      if (role === "embedding") roles.append(ollamaPanel);
    }
    setGatewayAnswers(fields.selections.answer);
    const embedding = element("gateway-embedding");
    embedding.addEventListener("change", () => {
      showOllamaSetup();
      if (embedding.value !== localEmbedding || gatewaySettings.configured || element("settings-key").value.trim()) return;
      element("gateway-answer").value = "";
      element("gateway-rerank").value = "";
      settingsDrafts[provider] = readSettingsFields(gateway);
    });
  } else {
    if (!genericSettings) return;
    const configured = genericSettings.configured;
    const setup = draft ?? (configured || genericSettings.defaults);
    element("settings-current").textContent = configured ? "답변 API 연결됨 · " + configured.model : "답변 API 연결 전";
    element("settings-remove").hidden = !configured;
    element("settings-key").placeholder = configured ? "같은 주소에서 키를 유지하려면 비워 두세요" : "";
    element("settings-url").value = setup.destination;
    element("settings-model").value = setup.model;
  }
  showOllamaSetup();
}

element("settings-open").addEventListener("click", async () => {
  if (settingsDialog.open) return;
  const version = ++settingsRequest;
  settingsMessage("");
  element("settings-current").textContent = "설정을 불러오는 중입니다.";
  element("settings-remove").hidden = true;
  element("settings-key").value = "";
  gatewayAdvanced.open = false;
  ollamaPanel.hidden = true;
  ++ollamaRequest;
  setSettingsBusy(true);
  settingsDialog.showModal();
  const loading = new AbortController();
  const timeout = setTimeout(() => loading.abort(), 10000);
  try {
    const [internal, external] = await Promise.all([
      call("/api/intranet/settings", undefined, loading.signal),
      call("/api/external/settings", undefined, loading.signal),
    ]);
    if (version !== settingsRequest || !settingsDialog.open) return;
    gatewaySettings = internal;
    genericSettings = external;
    settingsProvider.value = lastSettingsProvider ?? (
      external.configured && !internal.configured && internal.selections.embedding !== localEmbedding ? "external" : "intranet"
    );
    renderSettings();
    setSettingsBusy(false);
    element("settings-key").focus();
  } catch (error) {
    if (version !== settingsRequest || !settingsDialog.open) return;
    element("settings-current").textContent = "API 설정을 불러오지 못했습니다.";
    settingsMessage(error.name === "AbortError"
      ? "응답 시간이 초과되었습니다. 설정 창을 닫고 다시 열어 주세요."
      : error.message + " 설정 창을 닫고 다시 열어 주세요.");
  } finally {
    clearTimeout(timeout);
    loading.abort();
  }
});
settingsProvider.addEventListener("change", () => {
  lastSettingsProvider = settingsProvider.value;
  renderSettings();
  setSettingsBusy(false);
  settingsMessage("");
});
element("settings-close").addEventListener("click", () => settingsDialog.close());
settingsDialog.addEventListener("close", () => {
  if (!settingsDialog.open) {
    settingsRequest++;
    element("settings-key").value = "";
  }
});

async function changeSettings(action) {
  if (action !== "disconnect" && !settingsForm.reportValidity()) return;
  const version = settingsRequest;
  const provider = settingsProvider.value;
  const gateway = provider === "intranet";
  const submittedDraft = settingsDrafts[provider];
  const route = gateway ? "/api/intranet/settings" : "/api/external/settings";
  const payload = { action };
  if (action !== "disconnect") {
    Object.assign(payload, readSettingsFields(gateway));
    payload.key = payload.key.trim();
    if (gateway) {
      payload.test_unverified = action === "test" && payload.test_unverified;
      const { answer, embedding, rerank } = payload.selections;
      if (!payload.gateway_url.trim() && (payload.key || answer || rerank || (embedding && embedding !== localEmbedding))) {
        gatewayAdvanced.open = true;
        element("gateway-url").focus();
        settingsMessage(issueNames.gateway_url);
        return;
      }
    } else {
      payload.destination = payload.destination.trim();
      payload.model = payload.model.trim();
    }
  }
  setSettingsBusy(true);
  if (action !== "test") clearSearch();
  settingsMessage(action === "test" ? "선택한 모델의 연결을 확인하는 중입니다." : "");
  try {
    const data = await call(route, payload);
    if (action !== "test") {
      if (settingsDrafts[provider] === submittedDraft) delete settingsDrafts[provider];
      showSources().catch(() => {});
    }
    if (version !== settingsRequest || !settingsDialog.open) return;
    if (action === "test") {
      const checks = gateway ? Object.keys(gatewayRoles).filter(role => data.checks[role]).map(role => {
        const result = data.checks[role];
        const model = payload.selections[role] === localEmbedding ? "로컬 Ollama · BGE-M3" : payload.selections[role];
        const hint = gatewayHttpReasons[result.http_status];
        const reason = result.http_status ? "HTTP " + result.http_status + (hint ? " · " + hint : "")
          : gatewayCheckReasons[result.reason];
        return gatewayRoles[role][0] + " (" + model + ") " + (result.ok ? "성공" : "실패" + (reason ? " — " + reason : ""));
      }) : [];
      if (payload.test_unverified && checks.length) {
        checks.push("인증서 검증을 생략한 테스트입니다. 실제 사용 전에 체크를 해제하고 다시 테스트하세요.");
      }
      settingsMessage(gateway ? (checks.join(" · ") || "테스트할 모델을 선택하세요.") : "연결 테스트에 성공했습니다.");
      return;
    }
    if (gateway) gatewaySettings = data;
    else genericSettings = { ...genericSettings, configured: data.configured };
    renderSettings();
    settingsMessage(action === "disconnect" ? "연결을 해제하고 저장된 키를 지웠습니다."
      : gateway ? "설정을 적용했습니다. 임베딩 설정을 바꿨다면 보관함을 갱신하세요."
      : "설정을 적용했습니다. 검색 화면에서 답변 API를 선택할 수 있습니다.");
  } catch (error) {
    if (version === settingsRequest && settingsDialog.open) settingsMessage(error.message);
  } finally {
    if (version === settingsRequest && settingsDialog.open) setSettingsBusy(false);
  }
}

settingsForm.addEventListener("submit", (event) => {
  event.preventDefault();
  changeSettings("apply");
});
element("settings-test").addEventListener("click", () => changeSettings("test"));
element("settings-remove").addEventListener("click", () => changeSettings("disconnect"));

document.addEventListener("keydown", (event) => {
  if (event.key !== "/" || event.ctrlKey || event.metaKey || event.altKey || event.isComposing ||
      (element("viewer").open || settingsDialog.open) || event.target.closest?.("input, select, textarea, [contenteditable='true']")) return;
  event.preventDefault();
  element("query").focus();
});

element("query").addEventListener("keydown", (event) => {
  if (event.key !== "Enter" || !(event.ctrlKey || event.metaKey) || event.isComposing) return;
  event.preventDefault();
  form.requestSubmit(element("submit-ask"));
});

function showAddForm(open) {
  addForm.hidden = !open;
  addOpen.setAttribute("aria-expanded", open);
  (open ? sourcePath : addOpen).focus();
}
addOpen.addEventListener("click", () => showAddForm(addForm.hidden));
addForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = addForm.querySelector("button[type=submit]");
  const addNotice = element("add-notice");
  addNotice.textContent = "";
  button.disabled = true;
  try {
    await call("/api/sources", { path: sourcePath.value.trim().replace(/^"(.*)"$/, "$1") });
    sourcePath.value = "";
    showAddForm(false);
    notice("자료를 등록했습니다.");
    await showSources();
  } catch (error) {
    if (addForm.hidden) notice(error.message);
    else addNotice.textContent = error.message;
  } finally {
    button.disabled = false;
  }
});

async function pollSources() {
  try { await showSources(); } catch (error) { if (!sourceSnapshot) notice(error.message); }
  setTimeout(pollSources, 3000);
}
pollSources();

// Tell the server this page is still open; when every page closes, the app exits.
setInterval(() => { call("/api/ping").catch(() => {}); }, 2500);
