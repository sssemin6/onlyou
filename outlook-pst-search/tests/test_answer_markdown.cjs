const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const app = fs.readFileSync(path.join(__dirname, "../mailsearch/static/app.js"), "utf8");

function node(tag, text = "", className = "") {
  return {
    tag, textContent: text, className,
    children: [],
    events: {},
    classList: { remove() {} },
    setAttribute() {},
    addEventListener(type, handler) { this.events[type] = handler; },
    append(...children) { this.children.push(...children); },
    replaceChildren(...children) { this.children = children; },
  };
}

function loadFunctions(start, end, environment) {
  const browser = vm.createContext({ node, ...environment });
  vm.runInContext(app.slice(app.indexOf(start), app.indexOf(end)), browser);
  return browser;
}

test("asterisk and hyphen bullets share an unordered list while bold text stays in a paragraph", () => {
  const browser = loadFunctions("function appendMarkdownInline", "function showAnswer(data)");
  const answer = browser.renderAnswerMarkdown("* 첫 항목\n- 다음 항목\n\n**강조**", []);
  const [list, paragraph] = answer.children;
  assert.equal(list.tag, "ul");
  assert.deepEqual(list.children.map((item) => [item.tag, item.children.join("")]),
                   [["li", "첫 항목"], ["li", "다음 항목"]]);
  assert.equal(paragraph.tag, "p");
  assert.equal(paragraph.children[1].tag, "strong");
});

test("mail viewer compacts blank lines and marks only a supplied quote", async () => {
  const parts = {
    viewer: { ...node("dialog"), open: false, showModal() { this.open = true; } },
    "viewer-title": node("h2"),
    "viewer-content": node("div"),
  };
  const item = {
    kind: "mail", subject: "Mail", sender: "sender@example.invalid", recipient: "to@example.invalid",
    archive: "Archive", folder: "Inbox", attachments: [], text: "approval",
    body: "Line 1  \r\n\r\n   \r\n\r\napproval\r\n\r\n\r\nrecord  ",
  };
  const browser = loadFunctions("function highlight", "function recentTitle", {
    element: (id) => parts[id], call: async () => item, formatDate: () => "",
  });
  const original = item.body;
  await browser.openEvidence(1);
  let pre = parts["viewer-content"].children.find((child) => child.tag === "pre");
  assert.equal(pre.textContent, "Line 1\n\napproval\n\nrecord");
  assert.deepEqual(pre.children, []);
  await browser.openEvidence(1, "approval\r\n\r\n\r\nrecord  ");
  pre = parts["viewer-content"].children.find((child) => child.tag === "pre");
  assert.equal(pre.children[1].tag, "mark");
  assert.equal(pre.children[1].textContent, "approval\n\nrecord");
  assert.equal(item.body, original);
});

test("attachment matches and office previews open the full mail with a separate excerpt", async () => {
  const parts = {
    viewer: { ...node("dialog"), open: false, showModal() { this.open = true; } },
    "viewer-title": node("h2"),
    "viewer-content": node("div"),
  };
  const mail = {
    kind: "mail", subject: "Drawing review", archive: "Archive", folder: "Inbox",
    body: "Original mail introduction.\r\n\r\n\r\nReview approved. Full mail ending.",
    attachment: "Drawing.docx", attachments: [{ id: 3, name: "Drawing.docx", status: "완료", passage: 2 }],
  };
  const browser = loadFunctions("function highlight", "function recentTitle", {
    element: (id) => parts[id], formatDate: () => "",
    call: async (url) => url.endsWith("/1") ? { ...mail, location: "파일명", text: "Drawing.docx" }
      : url.endsWith("/2") ? { ...mail, location: "본문", text: "Attachment section: Review approved." }
        : { ...mail, body: "", location: "본문", text: " " },
  });
  const content = parts["viewer-content"];
  const headings = () => content.children.filter((child) => child.tag === "h3").map((child) => child.textContent);
  const paragraphs = () => content.children.filter((child) => child.tag === "pre");
  const original = "Original mail introduction.\n\nReview approved. Full mail ending.";

  await browser.openEvidence(1);
  assert.deepEqual(headings(), ["메일 본문", "첨부"]);
  assert.deepEqual(paragraphs().map((child) => child.textContent), [original]);

  const attachments = content.children.find((child) => child.tag === "ul");
  const preview = attachments.children[0].children.find((child) => child.textContent === "미리보기");
  await preview.events.click();
  assert.deepEqual(headings(), ["메일 본문", "일치한 첨부 내용 · Drawing.docx (본문)", "첨부"]);
  assert.deepEqual(paragraphs().map((child) => child.textContent), [original, "Attachment section: Review approved."]);

  await browser.openEvidence(2, "Review approved.");
  const [body, excerpt] = paragraphs();
  assert.equal(body.textContent, original);
  assert.deepEqual(body.children, []);
  assert.equal(excerpt.children[1].tag, "mark");
  assert.equal(excerpt.children[1].textContent, "Review approved.");

  await browser.openEvidence(4);
  assert.deepEqual(headings(), ["메일 본문", "첨부"]);
  assert.deepEqual(paragraphs().map((child) => child.textContent), ["본문 없음"]);
});

test("answer evidence leads with cited results and collapses extra results only for answers", () => {
  const parts = Object.fromEntries(["results", "result-section", "result-heading"].map((id) => [id, node("div")]));
  const browser = loadFunctions("function showResults", "function appendMarkdownInline", {
    element: (id) => parts[id], document: { createDocumentFragment: () => node("fragment") },
    formatDate: () => "2026-11-27",
  });
  const results = Array.from({ length: 7 }, (_, i) => ({
    passage: i, subject: "Mail " + i, preview: "a".repeat(420), kind: "mail", sender: "Sender",
    recipient: "Recipient", sent: "2026-11-27", attachment: "Plan.pdf",
  }));
  browser.showAnswerEvidence({ format: "markdown", sources: [{ passage: 6 }, { passage: 3 }, { passage: 6 }], results });
  const cards = parts.results.children[0].children;
  assert.deepEqual(cards.slice(0, 4).map((card) => card.children[0].textContent), ["Mail 6", "Mail 3", "Mail 0", "Mail 1"]);
  assert.equal(cards[4].tag, "details");
  assert.equal(cards[4].children[0].textContent, "근거 자료 3건 더 보기");
  assert.equal(cards[4].children.length, 4);
  assert.match(cards[0].children[1].textContent, /발신 Sender · 수신 Recipient/);
  assert.equal(cards[0].children.find((child) => child.className === "result-attachment").textContent, "첨부 Plan.pdf");
  assert.equal(cards[0].children.find((child) => child.className === "result-preview").textContent.length, 400);
  browser.showResults(results, "검색 결과");
  assert.equal(parts.results.children[0].children.length, 7);
});

test("mail cards summarize recipients and show attachments and a body fallback", () => {
  const parts = Object.fromEntries(["results", "result-section", "result-heading"].map((id) => [id, node("div")]));
  const browser = loadFunctions("function showResults", "function appendMarkdownInline", {
    element: (id) => parts[id], document: { createDocumentFragment: () => node("fragment") },
    formatDate: () => "2026-11-27",
  });
  browser.showResults([
    { kind: "mail", sender: "Sender", recipient: '"Lee, Mira" <mira@example.invalid>; Kim\nPark',
      attachment: "Drawing.pdf", attachments: ["Notes.txt", "Drawing.pdf"], location: "파일명",
      preview: "Drawing.pdf", body_preview: "The drawing approval is attached." },
    { kind: "document", subject: "Draft.xlsx", archive: "Files", folder: "Specs", location: "Sheet 1", preview: "Draft data" },
  ], "검색 결과");
  const [mail, doc] = parts.results.children[0].children;
  assert.match(mail.children[1].textContent, /발신 Sender · 수신 "Lee, Mira" <mira@example.invalid>, Kim 외 1명 · 2026-11-27/);
  assert.equal(mail.children[2].textContent, "첨부 Drawing.pdf (외 1건)");
  assert.equal(mail.children[2].title, "Notes.txt\nDrawing.pdf");
  assert.equal(mail.children.at(-1).textContent, "The drawing approval is attached.");
  assert.equal(doc.children[1].textContent, "2026-11-27 · Files · Specs · Sheet 1");
  assert.equal(doc.children[2].textContent, "Draft data");
});

test("activity tracks stage timings, collapses on completion and stops its timer on abort", () => {
  const box = node("div");
  let now = 0;
  let tick;
  const browser = loadFunctions("function startAnswerProgress", "async function intranetAnswer", {
    element: () => box, performance: { now: () => now },
    setInterval(fn, ms) { assert.equal(ms, 100); tick = fn; return 1; },
    clearInterval() { tick = undefined; },
  });
  const request = new AbortController();
  const progress = browser.startAnswerProgress(request.signal, "retrieving", "검색 중...");
  const [panel] = box.children;
  const [heading, steps] = panel.children;
  progress.update("retrieving", "검색 중...");
  now = 1250;
  tick();
  assert.equal(heading.children[2].textContent, "1.3초");
  progress.update("generating", "작성 중...");
  assert.equal(steps.children.length, 2);
  assert.equal(steps.children[0].textContent, "✓ 검색 중... (1.3초)");
  assert.equal(steps.children[1].textContent, "● 작성 중...");
  now = 2000;
  const completed = progress.finish();
  assert.equal(completed.tag, "details");
  assert.equal(completed.className, "answer-progress-done");
  assert.match(completed.children[0].textContent, /총 2.0초/);
  assert.equal(tick, undefined);
  browser.startAnswerProgress(request.signal, "retrieving", "다시 검색 중...");
  request.abort();
  assert.equal(tick, undefined);
});
