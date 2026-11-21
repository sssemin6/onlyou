"use strict";

const element = (id) => document.getElementById(id);
const token = document.querySelector('meta[name="mail-session"]').content;
const form = element("search-form");
const addForm = element("add-form");
const addToggle = element("add-toggle");
const settingsDialog = element("settings-dialog");
const settingsForm = element("settings-form");
const filterIds = ["subject", "body", "sender", "recipient", "attachment", "after", "before"];
const dateFormat = new Intl.DateTimeFormat("ko-KR", { dateStyle: "medium", timeStyle: "short" });
const stateNames = {
  queued: "대기", extracting: "PST 읽는 중", headers: "메일 색인 중", attachments: "첨부 색인 중",
  embeddings: "의미 검색 준비 중", ready: "검색 가능", paused: "일시정지", error: "오류", empty: "가져온 메일 없음",
  removing: "삭제 중", remove_failed: "삭제 재시도 필요",
};
const issueNames = {
  readpst_missing: "PST 추출 도구(readpst)를 설치하거나 실행 경로를 지정하세요.",
  pst_in_use: "Outlook에서 사용 중이거나 접근할 수 없는 PST입니다. Outlook을 닫거나 사본을 등록하세요.",
  pst_export: "PST 추출에 실패했습니다. Outlook에서 해당 파일이 열리는지, 로컬 디스크에 여유 공간이 있는지 확인하세요.",
  pst_invalid: "Outlook PST 헤더를 읽을 수 없습니다. Outlook에서 만든 .pst 파일인지 확인하세요.",
  pst_no_mail: "추출된 메일이 없습니다. Outlook에서 이 PST를 열어 받은 편지함이나 하위 폴더에 메일이 있는지 확인하세요.",
  eml_no_mail: "폴더에서 메일을 찾지 못했습니다. 폴더에 .eml 또는 .pst 파일이 있는지 확인하세요.",
  mail_parse_failed: "가져온 메일을 읽을 수 없습니다. 아래 실패 건수를 확인하고 다시 색인해 보세요.",
  readpst_dependencies: "readpst가 시작되지 않았습니다. MSYS2에서 함께 설치된 DLL이 있는지 확인하세요.",
  disk_or_source: "파일 접근 권한과 저장 공간을 확인하세요.",
  changed: "파일이 변경되었습니다. 갱신해 주세요.",
  interrupted: "중단된 색인입니다.",
  embedding_unavailable: "의미 검색 모델을 열지 못했습니다. 일반 검색은 사용할 수 있습니다.",
  external_disabled: "상단의 API 설정에서 외부 AI를 연결해 주세요.",
  external_url: "HTTPS 주소를 확인하세요. 주소는 /chat/completions로 끝나야 합니다.",
  external_model: "제공자에서 안내한 모델명을 입력하세요.",
  external_key: "API 키에 공백이 있는지 확인하세요.",
  external_key_required: "이 주소에서 사용할 API 키를 입력하세요.",
  external_test_auth: "인증이 거부되었습니다. API 키와 모델 사용 권한을 확인하세요.",
  external_test_endpoint: "요청이 거부되었습니다. 주소, 모델명, JSON 모드 지원 여부를 확인하세요.",
  external_test_limit: "제공자의 요청 한도 또는 잔액을 확인하세요.",
  external_test_failed: "연결 테스트에 실패했습니다. 네트워크와 제공자의 응답을 확인하세요.",
  external_busy: "외부 API가 다른 질문을 처리하고 있습니다. 잠시 후 다시 시도하세요.",
  external_review_expired: "전송 검토가 만료되었습니다. 질문을 다시 실행해 주세요.",
  external_source_changed: "검토한 메일의 보관함이 변경되었습니다. 질문을 다시 실행해 주세요.",
  source_path: "이 PC에서 읽을 수 있는 경로를 입력하세요.",
  source_format: "PST, EML 파일 또는 EML이 든 폴더만 등록할 수 있습니다.",
  source_busy: "현재 작업이 끝난 뒤 다시 시도하세요.",
  source_missing: "보관함을 찾지 못했습니다.",
  evidence_missing: "이 근거의 보관함이 갱신되었거나 접근 권한이 사라졌습니다.",
  invalid_input: "검색 범위와 입력 값을 확인하세요.",
  question_required: "질문을 입력하세요.",
  index_unavailable: "색인 DB에 접근하지 못했습니다. 잠시 후 다시 시도하세요.",
};
let previousQuestion = "";
let request;
let sourceSnapshot = "";
let sourceRequest = 0;
let settingsRequest = 0;

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
  const count = filterIds.filter((id) => element(id).value.trim()).length;
  const badge = element("filter-count");
  badge.textContent = count;
  badge.hidden = !count;
  element("clear-filters").hidden = !count;
}

async function showSources() {
  const version = ++sourceRequest;
  const data = await call("/api/sources");
  if (version !== sourceRequest) return;
  const next = JSON.stringify(data);
  if (next === sourceSnapshot) return;
  sourceSnapshot = next;
  const list = element("sources");
  const focusedAction = list.contains(document.activeElement) ? document.activeElement.dataset.action : "";
  list.replaceChildren();
  if (!data.sources.length) list.append(node("p", "등록한 파일이 없습니다.", "source-empty"));
  for (const source of data.sources) {
    const name = source.path.split(/[\\/]/).pop();
    const entry = node("div", "", "source-card");
    entry.dataset.state = source.state;
    const heading = node("h3", name, "source-name");
    heading.title = source.path;
    entry.append(heading, node("p", (stateNames[source.state] || source.state) + " · 메일 " + source.done.toLocaleString("ko-KR") + "건", "source-state"));
    if (source.failed || source.omitted || source.partial) {
      entry.append(node("p", "메일 실패 " + source.failed + " · 첨부 누락 " + source.omitted + " · 일부 " + source.partial));
    }
    if (source.updated) {
      const existing = source.active !== null && source.pending !== null;
      entry.append(node("p", (existing ? "이전 색인 사용 중 · " : "색인 기준 ") + formatDate(source.updated)));
    }
    if (source.issue) entry.append(node("p", issueNames[source.issue] || source.issue, "issue"));
    if (!source.available && source.state !== "removing") entry.append(node("p", "원본 경로 접근 불가", "issue"));
    const actions = node("div", "", "source-actions");
    const choices = ["queued", "extracting", "headers", "attachments", "embeddings"].includes(source.state)
      ? [["pause", "일시정지"]]
      : source.state === "ready" ? [["refresh", "갱신"]]
      : source.state === "removing" || source.state === "remove_failed" ? []
      : source.pending === null || ["pst_no_mail", "eml_no_mail", "mail_parse_failed"].includes(source.issue)
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
            request?.abort();
            element("answer").hidden = true;
            element("result-section").hidden = true;
            form.closest("main").classList.remove("has-query");
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
    list.append(entry);
  }
  showFilterCount();
  if (focusedAction) {
    const replacement = [...list.querySelectorAll("button")].find((button) => button.dataset.action === focusedAction);
    (replacement || addToggle).focus({ preventScroll: true });
  }
  element("provider-control").hidden = !data.external;
  if (!data.external) element("provider").value = "local";
  const modelStatus = element("model-status");
  modelStatus.parentElement.dataset.configured = Boolean(data.model);
  modelStatus.textContent = data.model
    ? "로컬 AI 설정됨 · " + data.model + (data.embedding ? " · 의미 검색" : "")
    : "로컬 AI 연결 전";
  const externalPanel = element("external-panel");
  externalPanel.dataset.configured = Boolean(data.external);
  element("external-status").textContent = data.external
    ? "외부 API 연결됨 · " + data.external.model
    : "외부 API 연결 전";
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
    const fields = [
      ["발신자", item.sender || "정보 없음"],
      ["수신자", item.recipient || "정보 없음"],
      ["날짜", formatDate(item.sent)],
      ["출처", item.archive + " · " + item.folder],
    ];
    if (item.attachment) fields.push(["첨부", item.attachment + " · " + item.location]);
    for (const [label, value] of fields) metadata.append(node("dt", label), node("dd", value));
    body.replaceChildren(metadata);
    if (quote) body.append(node("blockquote", quote));
    const text = item.attachment ? item.text : item.body || item.text;
    const original = highlight(text, quote || item.text);
    body.append(original);
    if (item.attachments.length) {
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

function showResults(rows, title) {
  const result = element("results");
  result.replaceChildren();
  element("result-section").hidden = false;
  element("result-heading").textContent = title + " (" + rows.length + ")";
  if (!rows.length) result.append(node("p", "이 범위에서 일치하는 메일을 찾지 못했습니다.", "result-empty"));
  for (const row of rows) {
    const button = node("button", "", "result");
    button.type = "button";
    button.append(node("strong", row.subject || "제목 없는 메일"),
      node("span", [row.sender, formatDate(row.sent), row.archive, row.folder, row.attachment, row.attachment ? row.location : ""].filter(Boolean).join(" · "), "result-meta"),
      node("span", row.preview, "result-preview"));
    button.addEventListener("click", () => openEvidence(row.passage));
    result.append(button);
  }
}

function showAnswer(data) {
  const box = element("answer");
  const statuses = {
    answered: "메일 근거로 찾은 답변", partial: "일부 근거만 확인했습니다",
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

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  request?.abort();
  const active = request = new AbortController();
  const query = element("query").value.trim();
  const ask = event.submitter?.value === "ask";
  const external = ask && element("provider").value === "external";
  if (ask && !query) {
    element("query").focus();
    notice("질문을 입력하세요.");
    return;
  }
  const filters = Object.fromEntries(filterIds.map((id) => [id, element(id).value]));
  const payload = { query, filters, context: ask ? previousQuestion : "" };
  element("answer").hidden = true;
  element("result-section").hidden = true;
  form.closest("main").classList.add("has-query");
  notice(ask ? "메일에서 근거를 찾고 있습니다." : "검색 중입니다.");
  try {
    if (external) {
      const review = await call("/api/external/preview", payload, active.signal);
      if (active.signal.aborted) return;
      if (!review.review) {
        showResults(review.results, "근거 후보");
        notice("외부 API로 보낼 수 있는 메일 발췌문을 찾지 못했습니다.");
        return;
      }
      notice("외부 API 응답을 기다리고 있습니다.");
      const answer = await call("/api/external/send",
        { review: review.review, excerpts: review.excerpts.map((item) => item.id) }, active.signal);
      if (active.signal.aborted) return;
      showAnswer(answer);
      if (answer.status !== "external_unavailable") showResults(answer.results, "전송한 발췌문의 원본 메일");
      rememberQuestion(answer, query);
      notice("");
      return;
    }
    if (ask) {
      const preliminary = await call("/api/candidates", payload, active.signal);
      if (active.signal.aborted) return;
      showResults(preliminary.results, "근거 후보");
      notice("로컬 AI가 답변을 작성하고 있습니다.");
      const data = await call("/api/answer", payload, active.signal);
      if (active.signal.aborted) return;
      showAnswer(data);
      showResults(data.results, "관련 메일");
      rememberQuestion(data, query);
    } else {
      const data = await call("/api/search", payload, active.signal);
      if (active.signal.aborted) return;
      showResults(data.results, query ? "검색 결과" : "최근 메일");
    }
    notice("");
  } catch (error) {
    if (!active.signal.aborted) notice(error.message);
  }
});

element("clear-context").addEventListener("click", () => {
  setQuestionContext("");
  element("query").focus();
});
for (const id of filterIds) element(id).addEventListener("input", showFilterCount);
element("clear-filters").addEventListener("click", () => {
  for (const id of filterIds) element(id).value = "";
  showFilterCount();
  element("source").focus();
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
  settingsMessage(action === "test" ? "테스트 문구로 연결을 확인하는 중입니다." : "");
  try {
    const data = await call("/api/external/settings", payload);
    if (action !== "test") {
      element("provider-control").hidden = !data.configured;
      if (!data.configured) element("provider").value = "local";
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
  addToggle.setAttribute("aria-expanded", open);
  if (open) element("path").focus();
}
addToggle.addEventListener("click", () => showAddForm(addForm.hidden));
addForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = addForm.querySelector("button[type=submit]");
  element("add-notice").textContent = "";
  button.disabled = true;
  try {
    await call("/api/sources", { path: element("path").value.trim().replace(/^"(.*)"$/, "$1") });
    element("path").value = "";
    showAddForm(false);
    addToggle.focus();
    notice("보관함을 등록했습니다.");
    await showSources();
  } catch (error) {
    if (addForm.hidden) notice(error.message);
    else element("add-notice").textContent = error.message;
  } finally {
    button.disabled = false;
  }
});

async function pollSources() {
  try { await showSources(); } catch (error) { if (!sourceSnapshot) notice(error.message); }
  setTimeout(pollSources, 3000);
}
pollSources();
