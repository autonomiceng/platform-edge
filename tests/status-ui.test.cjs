// UI policy checks without a DOM renderer; real browser acceptance remains separate.
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const S = require("../docker/console/status.js");
const fixture = (stack) =>
  fs.readFileSync(`docs/operations/status-fixtures/v2-${stack}.json`, "utf8");
function ui() {
  const element = {
    textContent: "{}",
    hidden: false,
    disabled: false,
    dataset: {},
    contains: () => false,
    addEventListener() {},
    querySelector: () => null,
  };
  const context = vm.createContext({
    StackStatus: S,
    location: { search: "", hostname: "localhost", protocol: "http:", port: "" },
    document: { addEventListener() {}, querySelector: () => element },
    setInterval() {},
    AbortSignal,
  });
  vm.runInContext(fs.readFileSync("docker/console/catalog.js", "utf8"), context);
  vm.runInContext(
    fs.readFileSync("docker/console/app.js", "utf8").replace(/check\(\);\s*$/, ""),
    context,
  );
  const run = (code) => vm.runInContext(code, context);
  run(
    `config = validateConfig({domain:'localhost', tailnet:'', root:'', apps:'console,litellm,langfuse,s3,rustfs,backplane,grafana', scheme:'http'});`,
  );
  run(`var app = (p, a) => find(p, a).app, state = (p, a) => appState(find(p).project, app(p, a));`);
  run(`var links = (p, a) => Object.fromEntries(endpoints(app(p, a)));`);
  const load = () => {
    for (const stack of Object.keys(S.ids))
      run(`documents.${stack} = StackStatus.parse(${JSON.stringify(fixture(stack))}, '${stack}');`);
  };
  return { run, load };
}

test("trusted links survive missing producers and failed reachability", () => {
  const { run } = ui();
  run(`health.backplane = { state: 'unreachable', detail: 'HTTP 502' };`);
  assert.equal(run(`state('backplane', 'backplane')`), "unknown", "missing producer");
  assert.equal(run(`links('backplane', 'backplane').URL`), "http://backplane.localhost/dashboard/");
  run(`location.hostname = 'platform.private.ts.net';`);
  assert.equal(run(`links('backplane', 'backplane').URL`), undefined);
  run(`config.tailnet = 'private.ts.net';`);
  assert.equal(run(`links('backplane', 'backplane').URL`), "https://backplane.private.ts.net/dashboard/");
  assert.equal(run(`links('gateway', 'litellm').API`), "https://litellm.private.ts.net");
  // A recorded tailnet makes the Tailnet Origins the browser URLs from the local address too.
  run(`location.hostname = 'localhost';`);
  assert.equal(run(`links('gateway', 'litellm').URL`), "https://litellm.private.ts.net/ui/");
  run(`config.root = 'edge'; location.hostname = 'platform.private.ts.net';`);
  assert.equal(run(`links('backplane', 'backplane').URL`), undefined);
  run(`location.hostname = 'edge.private.ts.net';`);
  assert.equal(run(`links('edge', 'caddy').URL`), "https://edge.private.ts.net/");
  // An application without a node keeps its public link locally and has none on the Tailnet console.
  run(`config.apps = ['console', 'litellm'];`);
  assert.equal(run(`links('backplane', 'backplane').URL`), undefined);
  assert.equal(run(`links('gateway', 'litellm').API`), "https://litellm.private.ts.net");
  run(`location.hostname = 'localhost';`);
  assert.equal(run(`links('backplane', 'backplane').URL`), "http://backplane.localhost/dashboard/");
  // S3 is an endpoint row only; the RustFS title opens its console.
  run(`config.apps = ['console', 's3', 'rustfs'];`);
  assert.deepEqual(run(`JSON.stringify(endpoints(app('gateway', 'rustfs')))`), JSON.stringify([
    ["URL", "https://rustfs.private.ts.net/rustfs/console/"],
    ["S3", "https://s3.private.ts.net"],
  ]));
  // Without the console's node on the tailnet console, the title has no link, never the S3 API.
  run(`config.apps = ['console', 's3']; location.hostname = 'edge.private.ts.net';`);
  assert.doesNotMatch(run(`appCard(find('gateway').project, app('gateway', 'rustfs'))`), /<a /);
});

test("every contract ID maps to exactly one catalog app or component", () => {
  const { run } = ui();
  for (const [stack, ids] of Object.entries(S.ids)) {
    const actual = JSON.parse(
      run(`JSON.stringify((p => [...p.apps.map(a => a.status), ...p.components.map(c => c.id)])(find('${stack}').project))`),
    );
    assert.deepEqual([...actual].sort(), [...ids].sort(), stack);
  }
});

test("badges: health decides reachability, the document decides Disabled", async () => {
  const { run, load } = ui();
  run(`health.litellm = { state: 'healthy' };`);
  assert.equal(run(`state('gateway', 'litellm')`), "unknown", "a 200 without a document is no evidence");
  load();
  for (const [status, expected] of [[200, "healthy"], [502, "unreachable"], [503, "unreachable"], [504, "unreachable"], [404, "unknown"]]) {
    run(`fetch = async () => ({ status: ${status} })`);
    await run(`probe('litellm')`);
    assert.equal(run(`state('gateway', 'litellm')`), expected, String(status));
  }
  run(`fetch = async () => { throw Object.assign(new Error('late'), { name: 'TimeoutError' }); }`);
  await run(`probe('litellm')`);
  assert.equal(run(`state('gateway', 'litellm')`), "unreachable");
  assert.equal(run(`health.litellm.detail`), "timed out");
  run(`health.s3 = { state: 'healthy' }; health.rustfs = { state: 'unreachable' };`);
  assert.equal(run(`state('gateway', 'rustfs')`), "degraded");
  run(`health.backplane = { state: 'healthy' };`);
  const doc = JSON.parse(fixture("backplane"));
  doc.components[0].enabled = false;
  run(`documents.backplane = StackStatus.parse(${JSON.stringify(JSON.stringify(doc))}, 'backplane');`);
  assert.equal(run(`state('backplane', 'backplane')`), "disabled");
  assert.equal(run(`reachable(find('backplane').project).join()`), "0,0");
});

test("versions, Disabled components and features come only from the document", () => {
  const { run, load } = ui();
  assert.equal(run(`versionText(find('edge').project, 'caddy')`), "Version unknown");
  load();
  assert.equal(run(`versionText(find('edge').project, 'caddy')`), "Configured 2.11.4");
  assert.equal(run(`versionText(find('gateway').project, 'litellm')`), "Configured v1.101.0");
  assert.equal(run(`versionText(find('backplane').project, 'workerd')`), "Configured");
  const drawer = run(`projectDrawer(find('gateway').project)`);
  assert.match(drawer, /Postgres exporter.*data-state="disabled"/);
  assert.doesNotMatch(drawer, /data-state="healthy"/, "supporting components are never probed");
  assert.match(drawer, /Configured · last checkpoint 2026-09-22 03:00 UTC/);
  assert.match(run(`projectDrawer(find('observability').project)`), /Alerts.*Not configured/);
  assert.match(run(`appDrawer(find('gateway').project, app('gateway', 'langfuse'))`), /<code>langfuse<wbr>\/langfuse:4<wbr>\.37\.0<\/code>/);
});

test("status URLs and text cannot become trusted navigation or HTML", () => {
  const { run } = ui();
  const doc = JSON.parse(fixture("backplane"));
  doc.components[0].url = "https://untrusted.test/";
  doc.components[0].name = "<script>alert(1)</script>";
  run(`documents.backplane = StackStatus.parse(${JSON.stringify(JSON.stringify(doc))}, 'backplane');`);
  assert.equal(run(`links('backplane', 'backplane').URL`), "http://backplane.localhost/dashboard/");
  const html = run(`DATA.projects.map(projectSection).join('') + appDrawer(find('backplane').project, app('backplane', 'backplane'))`);
  assert.doesNotMatch(html, /untrusted|<script>/);
  assert.equal(run(`esc('<script>')`), "&lt;script&gt;");
});
