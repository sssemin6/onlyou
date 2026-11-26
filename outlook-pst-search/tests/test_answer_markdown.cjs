const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

function node(tag) {
  return {
    tag,
    children: [],
    append(...children) { this.children.push(...children); },
  };
}

test("asterisk and hyphen bullets share an unordered list while bold text stays in a paragraph", () => {
  const app = fs.readFileSync(path.join(__dirname, "../mailsearch/static/app.js"), "utf8");
  const start = app.indexOf("function appendMarkdownInline");
  const end = app.indexOf("function showAnswer(data)", start);
  const browser = vm.createContext({ node });
  vm.runInContext(app.slice(start, end), browser);
  const answer = browser.renderAnswerMarkdown("* 첫 항목\n- 다음 항목\n\n**강조**", []);
  const [list, paragraph] = answer.children;
  assert.equal(list.tag, "ul");
  assert.deepEqual(list.children.map((item) => [item.tag, item.children.join("")]),
                   [["li", "첫 항목"], ["li", "다음 항목"]]);
  assert.equal(paragraph.tag, "p");
  assert.equal(paragraph.children[1].tag, "strong");
});
