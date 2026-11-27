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
    classList: { remove() {} },
    setAttribute() {},
    addEventListener() {},
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
  assert.match(cards[0].children[2].textContent, /수신 Recipient.*첨부 Plan.pdf/);
  assert.equal(cards[0].children[3].textContent.length, 400);
  browser.showResults(results, "검색 결과");
  assert.equal(parts.results.children[0].children.length, 7);
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
