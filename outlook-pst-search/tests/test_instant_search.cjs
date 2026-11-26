const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const app = fs.readFileSync(path.join(__dirname, "../mailsearch/static/app.js"), "utf8");

function setup() {
  const elements = new Map();
  const timers = new Map();
  const requests = [];
  let now = 0;
  let id = 0;

  function element() {
    const listeners = new Map();
    const attributes = new Map();
    return {
      value: "", hidden: true, children: [], dataset: {}, classList: { toggle() {} },
      addEventListener(type, handler) {
        listeners.set(type, [...(listeners.get(type) || []), handler]);
      },
      emit(type, details = {}) {
        const event = { target: this, preventDefault() {}, ...details };
        for (const handler of listeners.get(type) || []) handler(event);
      },
      append(...children) { this.children.push(...children); },
      replaceChildren(...children) { this.children = children; },
      contains(child) { return this === child || this.children.some((item) => item.contains(child)); },
      setAttribute(name, value) { attributes.set(name, value); },
      getAttribute(name) { return attributes.get(name); },
      removeAttribute(name) { attributes.delete(name); },
      scrollIntoView() {},
      focus() { document.activeElement = this; },
      requestSubmit(submitter) { this.emit("submit", { submitter }); },
    };
  }

  const document = Object.assign(element(), {
    getElementById(key) {
      if (!elements.has(key)) elements.set(key, element());
      return elements.get(key);
    },
    querySelector() { return { content: "token" }; },
    querySelectorAll() { return []; },
    createElement: element,
    createDocumentFragment: element,
  });

  const window = {
    setTimeout(callback, ms) {
      timers.set(++id, { at: now + ms, callback });
      return id;
    },
    clearTimeout(timer) { timers.delete(timer); },
  };
  const context = vm.createContext({
    document, window, AbortController, AbortSignal, setInterval() {},
    localStorage: { getItem: () => "mail", setItem() {} },
    fetch(route, { body, signal }) {
      if (route.startsWith("/api/sources")) return new Promise(() => {});
      return new Promise((resolve) => {
        requests.push({ route, body: body && JSON.parse(body), signal,
          respond: (data) => resolve({ ok: true, json: async () => data }) });
      });
    },
  });
  vm.runInContext(app, context);
  const get = document.getElementById;
  get("query").focus();
  get("submit-search").value = "search";
  get("submit-ask").value = "ask";
  return {
    get, requests, document,
    type(value, isComposing = false) {
      get("query").value = value;
      get("query").emit("input", { isComposing });
    },
    async advance(ms) {
      now += ms;
      for (const [key, timer] of timers) {
        if (now < timer.at) continue;
        timers.delete(key);
        timer.callback();
      }
      await new Promise(setImmediate);
    },
  };
}

test("typing debounces scoped suggestions and quick search, aborts stale work, and leaves AI explicit", async () => {
  const app = setup();
  app.get("search-quick").checked = true;
  app.get("source").value = "7";
  app.type("한", true);
  await app.advance(260);
  assert.equal(app.requests.length, 0);
  app.type("At");
  await app.advance(100);
  app.type("Atlas");
  await app.advance(179);
  assert.equal(app.requests.length, 0);
  await app.advance(1);
  const [suggestion] = app.requests;
  assert.equal(suggestion.route, "/api/suggest");
  assert.equal(suggestion.body.prefix, "Atlas");
  assert.equal(suggestion.body.filters.source, "7");
  await app.advance(79);
  assert.equal(app.requests.length, 1);
  await app.advance(1);
  const [, stale] = app.requests;
  assert.equal(stale.route, "/api/search");
  assert.equal(stale.body.mode, "quick");
  assert.equal(stale.body.defer_access, true);
  app.type("Quartz");
  assert.equal(stale.signal.aborted, true);
  assert.equal(suggestion.signal.aborted, true);
  await app.advance(260);
  const [, , currentSuggestion, current] = app.requests;
  current.respond({ mode: "quick", results: [{ subject: "Quartz diagram" }] });
  stale.respond({ mode: "quick", results: [{ subject: "Obsolete Atlas" }] });
  currentSuggestion.respond({ suggestions: [] });
  await app.advance(0);
  assert.equal(app.get("results").children[0].children[0].children[0].textContent, "Quartz diagram");
  assert.deepEqual(app.requests.map(({ route }) => route), [
    "/api/suggest", "/api/search", "/api/suggest", "/api/search",
  ]);
  app.get("query").emit("keydown", { key: "Enter", ctrlKey: true });
  assert.equal(app.requests.at(-1).route, "/api/external/preview");
});

test("keyboard and outside click control the dropdown without starting search in other modes", async () => {
  const app = setup();
  const input = app.get("query");
  const list = app.get("search-suggestions");
  const options = [
    { label: "Atlas planning", query: "Atlas planning", kind: "subject" },
    { label: "Atlas route", query: "Atlas route", kind: "subject" },
  ];
  app.type("At");
  await app.advance(260);
  assert.deepEqual(app.requests.map(({ route }) => route), ["/api/suggest"]);
  app.requests.at(-1).respond({ suggestions: options });
  await app.advance(0);
  input.emit("keydown", { key: "ArrowUp" });
  assert.equal(input.getAttribute("aria-activedescendant"), "search-suggestion-1");
  input.emit("keydown", { key: "ArrowDown" });
  assert.equal(input.getAttribute("aria-activedescendant"), "search-suggestion-0");
  input.emit("keydown", { key: "Escape" });
  assert.equal(list.hidden, true);
  assert.equal(input.value, "At");

  app.type("Atl");
  await app.advance(180);
  app.requests.at(-1).respond({ suggestions: options });
  await app.advance(0);
  assert.equal(list.hidden, false);
  app.document.emit("pointerdown", { target: app.get("notice") });
  assert.equal(list.hidden, true);

  app.type("Atla");
  await app.advance(180);
  app.requests.at(-1).respond({ suggestions: options });
  await app.advance(0);
  input.emit("keydown", { key: "ArrowDown" });
  input.emit("keydown", { key: "Enter" });
  assert.equal(input.value, "Atlas planning");
  assert.equal(list.hidden, true);
  assert.equal(app.requests.at(-1).route, "/api/search");
  assert.equal(app.requests.at(-1).body.query, "Atlas planning");
});
