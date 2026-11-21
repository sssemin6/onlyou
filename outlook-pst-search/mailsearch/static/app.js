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
  embedding_unavailable: "의미 검색 모델을 열지 못했습니다. 일반 검색은 사용할 수 있습니다.",
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
  source_path: "이 PC에서 읽을 수 있는 경로를 입력하세요.",
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
  modelStatus.textContent = data.embedding ? "로컬 의미 검색 · " + data.embedding : "로컬 키워드 검색";
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
    const text = item.attachment ? item.text : item.body || item.text;
    const original = highlight(text, quote || item.text);
    body.append(original);
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
    ambiguous: "여러 건이 해당합니다. 품번이나 기간을 질문에 추가해 주세요.",
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
    card.append(label, original, node("p", excerpt.text));
    excerpts.append(card);
  }
  reviewSend.disabled = false;
  reviewBox.hidden = false;
  reviewBox.scrollIntoView({ block: "nearest" });
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
      const data = await call(pcf ? "/api/pcf/preview" : "/api/external/preview", payload, active.signal);
      if (active.signal.aborted) return;
      if (data.review) {
        notice("");
        showReview(data, pcf);
      } else {
        showResults(data.results, "근거 후보");
        notice("답변에 보낼 발췌문을 찾지 못했습니다.");
      }
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

function setSettingsBusy(busy) {
  for (const control of settingsForm.querySelectorAll("input, button")) {
    if (control.id !== "settings-close") control.disabled = busy;
  }
}

function renderSettings(configured) {
  element("settings-current").textContent = configured
    ? "이 실행에 적용됨 · " + configured.model
    : "외부 API 연결 전";
  element("settings-remove").hidden = !configured;
  element("settings-key").placeholder = configured ? "같은 주소에서 키를 유지하려면 비워 두세요" : "";
}

element("settings-open").addEventListener("click", async () => {
  if (settingsDialog.open) return;
  const version = ++settingsRequest;
  settingsMessage("");
  element("settings-current").textContent = "설정을 불러오는 중입니다.";
  element("settings-remove").hidden = true;
  element("settings-url").value = "";
  element("settings-model").value = "";
  element("settings-key").value = "";
  setSettingsBusy(true);
  settingsDialog.showModal();
  try {
    const data = await call("/api/external/settings");
    if (version !== settingsRequest || !settingsDialog.open) return;
    const setup = data.configured || data.defaults;
    element("settings-url").value = setup.destination;
    element("settings-model").value = setup.model;
    renderSettings(data.configured);
    setSettingsBusy(false);
    element("settings-key").focus();
  } catch (error) {
    if (version === settingsRequest && settingsDialog.open) settingsMessage(error.message);
  }
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
  const payload = action === "disconnect" ? { action } : {
    action,
    destination: element("settings-url").value.trim(),
    model: element("settings-model").value.trim(),
    key: element("settings-key").value.trim(),
  };
  setSettingsBusy(true);
  if (action !== "test") clearSearch();
  settingsMessage(action === "test" ? "테스트 문구로 연결을 확인하는 중입니다." : "");
  try {
    const data = await call("/api/external/settings", payload);
    if (action !== "test") {
      showSources().catch(() => {});
    }
    if (version !== settingsRequest || !settingsDialog.open) return;
    if (action === "test") {
      settingsMessage("연결 테스트에 성공했습니다. 적용을 누르면 사용할 수 있습니다.");
      return;
    }
    element("settings-key").value = "";
    renderSettings(data.configured);
    if (data.configured) {
      element("settings-url").value = data.configured.destination;
      element("settings-model").value = data.configured.model;
    }
    settingsMessage(data.configured
      ? "설정을 적용했습니다. 검색 화면의 답변 제공자에서 외부 API를 선택할 수 있습니다."
      : "외부 API 연결을 해제했습니다.");
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
