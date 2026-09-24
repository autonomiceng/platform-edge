// UI policy checks without a DOM renderer; real browser acceptance remains separate.
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const S = require("../docker/console/status.js");
const now = Date.parse("2026-09-23T17:00:00Z");
const fixture = (stack) =>
  fs.readFileSync(`docs/operations/status-fixtures/v2-${stack}.json`, "utf8");
function ui() {
  const context = vm.createContext({
    StackStatus: S,
    URLSearchParams,
    Date: class extends Date {
      static now() {
        return now;
      }
    },
    location: {
      search: "",
      hostname: "localhost",
      protocol: "http:",
      port: "",
    },
    document: {
      addEventListener() {},
      querySelector() {
        return { textContent: "{}" };
      },
    },
    setInterval() {},
  });
  vm.runInContext(
    fs.readFileSync("docker/console/catalog.js", "utf8"),
    context,
  );
  vm.runInContext(
    fs
      .readFileSync("docker/console/app.js", "utf8")
      .replace(/check\(\);\s*$/, ""),
    context,
  );
  const run = (code) => vm.runInContext(code, context);
  run(
    `config = validateConfig({domain:'localhost', tailnet:'', root:'', apps:'console,litellm,langfuse,s3,rustfs,backplane,grafana', scheme:'http'});`,
  );
  return run;
}
test("trusted links survive missing producers and failed HTTP reachability", () => {
  const run = ui();
  run(`health.backplane = 'unavailable';`);
  assert.equal(
    run(`serviceState(DATA.services.find(s => s.id === 'bp'))`),
    "unknown",
  );
  assert.equal(
    run(`serviceEvidence(DATA.services.find(s => s.id === 'bp'))`),
    "Status unavailable · HTTP reachability: unavailable",
  );
  assert.equal(
    run(`serviceLinks(DATA.services.find(s => s.id === 'bp')).Console`),
    "http://backplane.localhost/dashboard/",
  );
  run(`location.hostname = 'platform.private.ts.net';`);
  assert.equal(
    run(`serviceLinks(DATA.services.find(s => s.id === 'bp')).Console`),
    undefined,
  );
  run(`config.tailnet = 'private.ts.net';`);
  assert.equal(
    run(`serviceLinks(DATA.services.find(s => s.id === 'bp')).Console`),
    "https://backplane.private.ts.net/dashboard/",
  );
  assert.equal(
    run(`serviceLinks(DATA.services.find(s => s.id === 'lite')).API`),
    "https://litellm.private.ts.net",
  );
  // A recorded tailnet makes the Tailnet Origins the browser URLs from the local address too.
  run(`location.hostname = 'localhost';`);
  assert.equal(run(`serviceLinks(DATA.services.find(s => s.id === 'lite')).Console`), "https://litellm.private.ts.net/ui/");
  run(`config.root = 'edge'; location.hostname = 'platform.private.ts.net';`);
  assert.equal(run(`serviceLinks(DATA.services.find(s => s.id === 'bp')).Console`), undefined);
  run(`location.hostname = 'edge.private.ts.net';`);
  assert.equal(run(`serviceLinks(DATA.services.find(s => s.id === 'bp')).Console`), "https://backplane.private.ts.net/dashboard/");
  // An application without a node keeps its public link locally and has none on the Tailnet console.
  run(`config.apps = ['console', 'litellm'];`);
  assert.equal(run(`serviceLinks(DATA.services.find(s => s.id === 'bp')).Console`), undefined);
  assert.equal(run(`serviceLinks(DATA.services.find(s => s.id === 'lite')).API`), "https://litellm.private.ts.net");
  run(`location.hostname = 'localhost';`);
  assert.equal(run(`serviceLinks(DATA.services.find(s => s.id === 'bp')).Console`), "http://backplane.localhost/dashboard/");
  // Optional storage consoles are never linked; they are not Tailnet Origins.
  for (const service of ["b-rust", "o-rust"])
    assert.equal(run(`serviceLinks(DATA.services.find(s => s.id === '${service}')).Console`), undefined);
});
test("every contract ID maps to exactly one catalog component", () => {
  const run = ui();
  for (const [stack, ids] of Object.entries(S.ids)) {
    const actual = JSON.parse(
      run(
        `JSON.stringify(DATA.services.filter(s => s.project === '${stack}').map(statusKey))`,
      ),
    );
    for (const id of ids)
      assert.equal(actual.filter((key) => key === id).length, 1, `${stack}/${id}`);
  }
});
test("configured versions, Not enabled and features come only from the document", () => {
  const run = ui();
  for (const stack of Object.keys(S.ids))
    run(
      `statusDocuments.${stack} = StackStatus.parse(${JSON.stringify(fixture(stack))}, '${stack}');`,
    );
  const service = (id, expression) =>
    run(`${expression}(services().find(s => s.id === '${id}'))`);
  assert.equal(service("edge", "serviceVersion"), "Configured 2.11.4");
  assert.equal(service("edge", "state"), "Configured");
  assert.equal(service("lite", "serviceVersion"), "Configured v1.101.0");
  assert.equal(service("pg-export", "state"), "Not enabled");
  assert.equal(service("b-rust", "state"), "Not enabled");
  assert.equal(service("workerd", "serviceVersion"), "");
  assert.equal(service("g-init", "state"), "Unknown");
  assert.equal(
    service("grafana", "serviceEvidence"),
    "Configuration 2026-09-23T16:05:00.000Z · HTTP reachability: checking",
  );
  const cards = run(`ProjectCards()`);
  assert.match(cards, /Backups configured · no checkpoint recorded · Alerts not configured/);
  assert.doesNotMatch(cards, /healthy|Telemetry/);
});
test("app HTTP 200 never promotes a component to healthy", () => {
  const run = ui();
  run(`for (const id of Object.values(probes)) health[id] = 'reachable';`);
  assert.equal(
    run(`DATA.services.every(s => serviceState(s) === 'unknown')`),
    true,
  );
  run(`statusDocuments.gateway = StackStatus.parse(${JSON.stringify(fixture("gateway"))}, 'gateway');`);
  assert.equal(
    run(`DATA.services.some(s => serviceState(s) === 'healthy')`),
    false,
  );
});
test("status URLs and text cannot become trusted navigation or HTML", () => {
  const run = ui();
  const doc = JSON.parse(fixture("backplane"));
  doc.components[0].url = "https://untrusted.test/";
  doc.components[0].name = "<script>alert(1)</script>";
  run(
    `statusDocuments.backplane = StackStatus.parse(${JSON.stringify(JSON.stringify(doc))}, 'backplane');`,
  );
  assert.equal(
    run(`serviceLinks(DATA.services.find(s => s.id === 'bp')).Console`),
    "http://backplane.localhost/dashboard/",
  );
  assert.doesNotMatch(run(`ProjectCards()`), /untrusted|<script>/);
  assert.equal(run(`esc('<script>')`), "&lt;script&gt;");
});

test("capabilities never claim their own process log stream", () => {
  const run = ui();
  assert.equal(run(`DATA.services.find(s => s.id === 'files').kind`), "capability");
  assert.doesNotMatch(run(`selected = 'files'; detail()`), /Logs/);
});
