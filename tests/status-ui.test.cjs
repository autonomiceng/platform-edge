// UI policy checks without a DOM renderer; real browser acceptance remains separate.
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const S = require("../docker/console/status.js");
const now = Date.parse("2026-09-20T12:00:00Z");
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
    `config = validateConfig({domain:'localhost', tailscale:'', connected:'', scheme:'http', ports:{}});`,
  );
  return run;
}
test("trusted links survive missing producers and failed HTTP reachability", () => {
  const run = ui();
  run(`health.backplane = 'unavailable'; statusFailures.backplane = true;`);
  assert.equal(
    run(`serviceState(DATA.services.find(s => s.id === 'bp'))`),
    "unknown",
  );
  assert.equal(
    run(`serviceLinks(DATA.services.find(s => s.id === 'bp')).Console`),
    "http://backplane.localhost/dashboard/",
  );
  run(
    `config.tailscale = 'private.test.ts.net'; location.hostname = config.tailscale; config.ports.backplane = '8448';`,
  );
  assert.equal(
    run(`serviceLinks(DATA.services.find(s => s.id === 'bp')).Console`),
    undefined,
  );
  run(`config.connected = 'backplane';`);
  assert.equal(
    run(`serviceLinks(DATA.services.find(s => s.id === 'bp')).Console`),
    "https://private.test.ts.net:8448/dashboard/",
  );
});
test("optional storage console links require a configured Tailnet endpoint", () => {
  const run = ui();
  for (const [service, id, port] of [["b-rust", "backplane_rustfs", "8450"], ["o-rust", "observability_rustfs", "8451"]]) {
    assert.equal(run(`serviceLinks(DATA.services.find(s => s.id === '${service}')).Console`), undefined);
    run(`config.tailscale = 'private.test.ts.net'; location.hostname = config.tailscale; config.ports.${id} = '${port}';`);
    assert.equal(run(`serviceLinks(DATA.services.find(s => s.id === '${service}')).Console`), undefined);
    run(`config.connected += ',${id}';`);
    assert.equal(run(`serviceLinks(DATA.services.find(s => s.id === '${service}')).Console`), `https://private.test.ts.net:${port}/rustfs/console/`);
    run(`location.hostname = 'untrusted.test';`);
    assert.equal(run(`serviceLinks(DATA.services.find(s => s.id === '${service}')).Console`), undefined);
  }
});
test("every contract ID maps to exactly one catalog component", () => {
  const run = ui();
  for (const [stack, ids] of Object.entries(S.ids)) {
    const actual = JSON.parse(
      run(
        `JSON.stringify(DATA.services.filter(s => s.project === '${stack}').map(s => s.statusId || statusIds[s.id] || s.id).sort())`,
      ),
    );
    assert.deepEqual(actual, [...ids].sort());
  }
});
test("fresh per-ID configuration wins; expiry falls back and langfuse maps both components", () => {
  const run = ui();
  const doc = {
    schemaVersion: 1,
    stack: "gateway",
    generatedAt: "2026-09-20T12:00:00Z",
    configurationObservedAt: "2026-09-20T11:59:45Z",
    configurationValidForSeconds: 60,
    telemetry: "unknown",
    components: [
      {
        id: "langfuse-web",
        kind: "service",
        configured: true,
        state: "unknown",
        observedAt: null,
        validForSeconds: 60,
        configuredVersion: "3.2",
      },
    ],
  };
  run(
    `legacyVersions = {images:{langfuse:'3.1'}}; statusDocuments.gateway = StackStatus.parse(${JSON.stringify(JSON.stringify(doc))}, 'gateway', Date.now());`,
  );
  const version = (id) =>
    run(`serviceVersion(DATA.services.find(s => s.id === '${id}'))`);
  assert.match(version("langfuse"), /^Configured 3.2/);
  assert.equal(version("lf-worker"), "Configured 3.1 · undated");
  run(`statusDocuments.gateway.configurationValidForSeconds = 1;`);
  assert.equal(version("langfuse"), "Configured 3.1 · undated");
  assert.equal(version("lf-worker"), "Configured 3.1 · undated");
  assert.doesNotMatch(version("langfuse"), /Observed/);
  run(`legacyVersions.images.undefined = "must-not-map";`);
  assert.equal(version("gateway-bootstrap"), "");
});
test("app HTTP 200 never promotes sibling Caddy, databases, workers or capabilities", () => {
  const run = ui();
  run(`for (const id of Object.values(probes)) health[id] = 'reachable';`);
  assert.equal(
    run(`DATA.services.every(s => serviceState(s) === 'unknown')`),
    true,
  );
});
test("status URLs and text cannot become trusted navigation or HTML", () => {
  const run = ui();
  const fixture = JSON.parse(
    fs.readFileSync("docs/operations/status-fixtures/current.json", "utf8"),
  );
  fixture.components[0].url = "https://untrusted.test/";
  fixture.components[0].observedVersion = "<script>alert(1)</script>";
  run(
    `statusDocuments.backplane = StackStatus.parse(${JSON.stringify(JSON.stringify(fixture))}, 'backplane', Date.now());`,
  );
  assert.equal(
    run(`serviceLinks(DATA.services.find(s => s.id === 'bp')).Console`),
    "http://backplane.localhost/dashboard/",
  );
  assert.doesNotMatch(
    run(`serviceVersion(DATA.services.find(s => s.id === 'bp'))`),
    /script|Observed/,
  );
  assert.equal(run(`esc('<script>')`), "&lt;script&gt;");
});

test("capabilities never claim their own process log stream", () => {
  const run = ui();
  for (const id of ["files", "backplane-functions"]) {
    assert.equal(run(`DATA.services.find(s => s.id === '${id}').kind`), "capability");
  }
});
