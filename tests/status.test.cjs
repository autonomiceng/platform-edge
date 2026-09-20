const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const S = require("../docker/console/status.js");
const now = Date.parse("2026-09-20T12:00:00Z");
const fixture = (name) =>
  fs.readFileSync(`docs/operations/status-fixtures/${name}.json`, "utf8");
const input = () => JSON.parse(fixture("current"));
const parse = (d, time = now, date = null) =>
  S.parse(JSON.stringify(d), d.stack, time, date);
const view = (d, id = "server", time = now) => S.view(parse(d), id, time);

test("contract fixtures isolate malformed components and ignore additive fields", () => {
  for (const name of ["current", "forward-compatible", "malformed-component"]) {
    const d = S.parse(fixture(name), "backplane", now);
    assert.equal(S.view(d, "server", now).state, "healthy");
    if (name !== "current")
      assert.equal(S.view(d, "postgres", now).state, "unknown");
    assert.equal(S.view(d, "future-component", now).state, "unknown");
  }
  assert.throws(
    () => S.parse(fixture("duplicate"), "backplane", now),
    /Duplicate/,
  );
  assert.throws(
    () => S.parse(fixture("unsupported"), "edge", now),
    /Unsupported/,
  );
  const stale = S.parse(fixture("stale"), "edge", now);
  assert.equal(S.view(stale, "caddy", now).state, "unknown");
  assert.equal(S.view(stale, "caddy", now).reason, "Stale observation");
});
test("envelope errors reject whole document including duplicate unknown IDs", () => {
  const mutations = [
    (d) => (d.schemaVersion = "1"),
    (d) => (d.configurationValidForSeconds = 301),
    (d) => (d.generatedAt = null),
    (d) => (d.configurationObservedAt = "2026-02-31T00:00:00Z"),
    (d) => (d.telemetry = "healthy"),
    (d) => (d.components = {}),
    (d) => (d.components = Array(33).fill({})),
    (d) => d.components.push({ id: "future" }, { id: "future" }),
    (d) => delete d.configurationObservedAt,
  ];
  for (const mutate of mutations) {
    const d = input();
    mutate(d);
    assert.throws(() => parse(d));
  }
  assert.throws(() => S.parse(fixture("current"), "gateway", now));
  assert.throws(() => S.parse("{", "edge", now));
  assert.throws(() => S.parse(" ".repeat(65537), "edge", now));
});
test("invalid required component fields discard only that component", () => {
  const mutations = [
    (c) => delete c.observedAt,
    (c) => (c.observedAt = "2026-09-20T12:00:06Z"),
    (c) => (c.observedAt = "2026-02-31T00:00:00Z"),
    (c) => (c.observedAt = null),
    (c) => (c.observedAt = "2026-09-20T11:59:58+01:00"),
    (c) => (c.kind = "capability"),
    (c) => (c.configured = "true"),
    (c) => (c.configured = false),
    (c) => (c.validForSeconds = 0),
    (c) => (c.validForSeconds = 1.5),
    (c) => (c.state = "running"),
  ];
  for (const mutate of mutations) {
    const d = input();
    mutate(d.components[0]);
    assert.equal(view(d).state, "unknown");
    assert.equal(view(d, "postgres").state, "healthy");
  }
});
test("clock agreement is symmetric, uses Date when present and never renews evidence", () => {
  for (const delta of [-6000, 6000]) {
    const d = parse(input(), now, new Date(now + delta).toUTCString());
    assert.equal(S.view(d, "server", now).reason, "Clock disagreement");
    assert.equal(S.view(d, "server", now).observedVersion, null);
  }
  for (const delta of [-5000, 5000])
    assert.equal(
      S.view(
        parse(input(), now, new Date(now + delta).toUTCString()),
        "server",
        now,
      ).state,
      "healthy",
    );
  assert.equal(
    S.view(parse(input(), now, "invalid"), "server", now).reason,
    "Clock disagreement",
  );
  const d = input();
  d.generatedAt = "2026-09-20T10:00:00Z";
  d.configurationObservedAt = null;
  d.telemetry = "unknown";
  d.components = [];
  assert.equal(parse(d).clockAgrees, false);
  assert.equal(parse(d, now, new Date(now).toUTCString()).clockAgrees, true);
  d.generatedAt = "2026-09-20T12:00:06Z";
  assert.equal(parse(d).clockAgrees, false);
  assert.throws(() => parse(d, now, new Date(now).toUTCString()));
});
test("generation bounds apply even with an agreeing HTTP clock", () => {
  const d = input();
  d.generatedAt = "2026-09-20T11:59:00Z";
  d.configurationObservedAt = null;
  d.telemetry = "unknown";
  d.components[0].configured = null;
  d.components[0].state = "unknown";
  d.components[0].configuredVersion = null;
  const doc = parse(d, now, new Date(now).toUTCString());
  assert.equal(doc.components.server, undefined);
});
test("fresh task inspection does not age its execution; missing execution is unknown", () => {
  const d = input();
  const task = d.components.find((c) => c.id === "bootstrap");
  assert.equal(view(d, "bootstrap").state, "healthy");
  assert.equal(
    view(d, "bootstrap").lastExecutionAt,
    Date.parse("2026-09-01T10:00:00Z"),
  );
  task.lastExecutionAt = null;
  assert.equal(view(d, "bootstrap").state, "unknown");
  task.state = "unknown";
  assert.ok(parse(d).components.bootstrap);
  delete task.lastExecutionAt;
  assert.equal(parse(d).components.bootstrap, undefined);
  task.lastExecutionAt = "2026-09-20T12:00:06Z";
  assert.equal(parse(d).components.bootstrap, undefined);
});
test("disabled and configuration expire independently of observed versions", () => {
  const d = input();
  d.configurationValidForSeconds = 20;
  const before = view(d, "functions");
  assert.equal(before.state, "disabled");
  const after = view(d, "functions", now + 6000);
  assert.equal(after.state, "unknown");
  assert.equal(after.configured, null);
  assert.equal(after.telemetry, "unknown");
  const service = view(d, "postgres", now + 6000);
  assert.equal(service.state, "healthy");
  assert.equal(service.configuredVersion, null);
  assert.equal(service.observedVersion, "18.6");
  d.components.find((c) => c.id === "functions").observedAt = d.generatedAt;
  assert.equal(view(d, "functions").state, "unknown");
});
test("unknown/null and optional versions/digests never infer runtime versions", () => {
  const d = input();
  const c = d.components[0];
  assert.equal(view(d).observedVersion, null);
  assert.equal(view(d).configuredVersion, "custom");
  c.configuredVersion = "<script>";
  c.observedVersion = "x".repeat(129);
  c.configuredDigest = "sha256:short";
  c.observedImageId = "sha256:" + "a".repeat(64);
  assert.equal(view(d).configuredVersion, null);
  assert.equal(view(d).observedVersion, null);
  assert.equal(view(d).configuredDigest, null);
  assert.equal(view(d).observedImageId, c.observedImageId);
  d.configurationObservedAt = null;
  assert.throws(() => parse(d));
  d.telemetry = "unknown";
  c.configured = null;
  c.state = "unknown";
  c.observedAt = null;
  assert.ok(parse(d).components.server);
  assert.equal(view(d).observedImageId, null);
});
test("failed refresh and unchanged documents never extend observation age", () => {
  const d = input(),
    doc = parse(d);
  const failed = S.view(doc, "server", now, true);
  assert.equal(failed.state, "unknown");
  assert.match(failed.reason, /unavailable.*stale/);
  assert.equal(failed.observedAt, Date.parse(d.components[0].observedAt));
  assert.equal(S.view(doc, "server", now + 60000).state, "unknown");
  const frozen = parse(d, now + 60000, new Date(now + 60000).toUTCString());
  assert.equal(S.view(frozen, "server", now + 60000).state, "unknown");
});
test("legacy versions are explicitly configured, validly dated or undated", () => {
  const d = { images: { langfuse: "3.1" } };
  assert.equal(S.legacy(d, "langfuse", now), "Configured 3.1 · undated");
  d.pinnedAt = "2026-09-01T00:00:00Z";
  assert.match(S.legacy(d, "langfuse", now), /2026-09-01/);
  d.configuredAt = "invalid";
  assert.equal(S.legacy(d, "langfuse", now), null);
  d.configuredAt = "2026-09-21T00:00:00Z";
  assert.equal(S.legacy(d, "langfuse", now), null);
  d.configuredAt = "2026-09-20T11:59:59Z";
  assert.match(S.legacy(d, "langfuse", now), /2026-09-20/);
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
