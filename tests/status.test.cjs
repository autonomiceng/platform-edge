const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const S = require("../docker/console/status.js");
const fixture = (stack) =>
  fs.readFileSync(`docs/operations/status-fixtures/v2-${stack}.json`, "utf8");
const input = (stack = "gateway") => JSON.parse(fixture(stack));
const parse = (d) => S.parse(JSON.stringify(d), d.stack);

test("every stack fixture parses; disabled is off and versions stay configured", () => {
  for (const [stack, ids] of Object.entries(S.ids)) {
    const doc = S.parse(fixture(stack), stack);
    assert.deepEqual(Object.keys(doc.components).sort(), [...ids].sort());
    for (const c of input(stack).components)
      assert.deepEqual(S.view(doc, c.id), {
        state: c.enabled ? "configured" : "off",
        version: c.version,
        reason: `Configuration ${new Date(input(stack).configuredAt).toISOString()}`,
      });
  }
  const edge = S.parse(fixture("edge"), "edge");
  assert.equal(S.view(edge, "caddy").version, "2.11.4");
  assert.equal(S.features(edge), "Backups configured · no checkpoint recorded");
  assert.equal(
    S.features(S.parse(fixture("observability"), "observability")),
    "Backups configured · no checkpoint recorded · Alerts not configured",
  );
  assert.equal(
    S.features(S.parse(fixture("gateway"), "gateway")),
    "Backups configured · last checkpoint 2026-09-22T03:00:00.000Z",
  );
});
test("a missing producer or unknown ID is unknown, never healthy", () => {
  assert.deepEqual(S.view(undefined, "caddy"), {
    state: "unknown",
    version: null,
    reason: "Status unavailable",
  });
  assert.equal(S.features(undefined), "");
  const d = input();
  d.components.push({ ...d.components[0], id: "future", health: "/health/future" });
  const doc = parse(d);
  assert.equal(doc.components.future, undefined);
  delete d.features.backups;
  assert.equal(S.features(parse(d)), "");
  assert.equal(S.view(parse(d), "clickhouse").state, "configured");
});
test("contract, stack, closed fields, duplicates and limits reject the whole document", () => {
  const mutations = [
    (d) => (d.contract = 1),
    (d) => (d.contract = "2"),
    (d) => (d.schemaVersion = 1),
    (d) => (d.stack = "backplane"),
    (d) => delete d.features,
    (d) => (d.configuredAt = "2026-02-31T00:00:00Z"),
    (d) => (d.configuredAt = "2026-09-23T17:00:00+01:00"),
    (d) => (d.components = {}),
    (d) => (d.components = Array(33).fill({})),
    (d) => (d.components[1].observedVersion = "1.0.0"),
    (d) => d.components.push({ id: "future" }, { id: "future" }),
    (d) => d.components.push({ ...d.components[0] }),
    (d) => (d.features.telemetry = "configured"),
    (d) => (d.features.backups.path = "/srv/backups"),
  ];
  for (const mutate of mutations) {
    const d = input();
    mutate(d);
    assert.throws(() => S.parse(JSON.stringify(d), "gateway"));
  }
  assert.throws(() => S.parse(fixture("gateway"), "edge"));
  assert.throws(() => S.parse(fixture("edge"), "unknown"));
  assert.throws(() => S.parse("{", "edge"));
  assert.throws(() => S.parse(" ".repeat(65537), "edge"), /too large/);
});
test("an invalid component field discards only that component", () => {
  const mutations = [
    (c) => delete c.enabled,
    (c) => (c.enabled = "true"),
    (c) => (c.kind = "service"),
    (c) => (c.name = ""),
    (c) => (c.name = "x".repeat(65)),
    (c) => (c.image = c.image + "@sha256:" + "a".repeat(64)),
    (c) => (c.version = "<script>"),
    (c) => delete c.version,
    (c) => (c.health = "/health"),
    (c) => (c.url = "javascript:alert(1)"),
    (c) => (c.url = null),
    (c) => (c.url = "https://user:secret@litellm.example.com"),
    (c) => (c.url = "https://litellm.example.com/?token=x"),
  ];
  for (const mutate of mutations) {
    const d = input();
    mutate(d.components[1]);
    const doc = parse(d);
    assert.equal(S.view(doc, "litellm").state, "unknown");
    assert.equal(S.view(doc, "litellm").version, null);
    assert.equal(S.view(doc, "postgres").state, "configured");
  }
  const d = input();
  d.features.backups.lastCheckpointAt = "yesterday";
  d.features.alerts = { configured: "no" };
  assert.equal(S.features(parse(d)), "");
});
test("transport rejects 404/HTML/oversize and bounds actual streamed UTF-8 bytes", async () => {
  const response =
    (body, options = {}) =>
    async () =>
      new Response(body, {
        headers: { "Content-Type": "application/json" },
        ...options,
      });
  for (const fetcher of [
    response("{}", { status: 404 }),
    response("<html>", { headers: { "Content-Type": "text/html" } }),
    response("é".repeat(32769)),
    response("{}", {
      headers: {
        "Content-Type": "application/json",
        "Content-Length": "65537",
      },
    }),
  ])
    await assert.rejects(S.request("/status", fetcher));
  const result = await S.request("/status", async (url, options) => {
    assert.equal(options.credentials, "omit");
    assert.equal(options.redirect, "error");
    assert.equal(options.cache, "no-store");
    return new Response(" ".repeat(65536), {
      headers: { "Content-Type": "application/json; charset=utf-8" },
    });
  });
  assert.equal(result.text.length, 65536);
});
test("deadline covers a body that never completes", async () => {
  let cancelled = false;
  const body = new ReadableStream({
    cancel() {
      cancelled = true;
    },
  });
  await assert.rejects(
    S.request(
      "/status",
      async () =>
        new Response(body, { headers: { "Content-Type": "application/json" } }),
    ),
    /deadline/,
  );
  assert.equal(cancelled, true);
});
test("deadline cancels an ongoing trickle of body chunks", async () => {
  let chunks = 0, cancelled = false, interval;
  const body = new ReadableStream({
    start(controller) {
      interval = setInterval(() => {
        chunks++;
        controller.enqueue(new TextEncoder().encode(" "));
      }, 50);
    },
    cancel() {
      cancelled = true;
      clearInterval(interval);
    },
  });
  const started = Date.now();
  try {
    await assert.rejects(S.request("/status", async () => new Response(body, {
      headers: { "Content-Type": "application/json" },
    })), /deadline/);
    assert.ok(chunks > 1);
    assert.ok(Date.now() - started < 6000);
    assert.equal(cancelled, true);
  } finally {
    clearInterval(interval);
  }
});
test("bounded independent jobs survive a rejected producer", async () => {
  let active = 0,
    peak = 0,
    completed = 0;
  await S.pool(
    Array.from({ length: 8 }, (_, i) => async () => {
      active++;
      peak = Math.max(peak, active);
      await new Promise((resolve) => setTimeout(resolve, 5));
      active--;
      if (i === 0) throw Error();
      completed++;
    }),
  );
  assert.equal(peak, 3);
  assert.equal(completed, 7);
});
