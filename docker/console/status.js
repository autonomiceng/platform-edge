// Public status contract 2. Keep parsing and transport independent of the DOM.
const StackStatus = (() => {
  const MAX_BYTES = 65536;
  const DEADLINE_MS = 4000;
  const ids = {
    edge: ["caddy"],
    gateway: [
      "caddy",
      "litellm",
      "langfuse-web",
      "langfuse-worker",
      "postgres",
      "clickhouse",
      "valkey",
      "rustfs",
      "postgres-exporter",
      "valkey-exporter",
    ],
    backplane: ["server", "postgres", "rustfs", "workerd", "caddy"],
    observability: ["caddy", "grafana", "alloy", "loki", "mimir", "tempo", "rustfs"],
  };
  const envelope = ["contract", "stack", "configuredAt", "components", "features"];
  const fields = ["id", "name", "kind", "enabled", "image", "version", "health", "url"];
  const kinds = ["app", "datastore", "gateway", "collector", "runtime"];
  const object = (value) =>
    value !== null && typeof value === "object" && !Array.isArray(value);
  const only = (value, keys) => Object.keys(value).every((k) => keys.includes(k));
  const text = (value, max) =>
    typeof value === "string" && value.length >= 1 && value.length <= max;
  function timestamp(value) {
    if (
      typeof value !== "string" ||
      !/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|\+00:00)$/.test(value)
    )
      return NaN;
    const time = Date.parse(value);
    // Date.parse silently normalizes nonexistent calendar dates.
    return Number.isFinite(time) &&
      new Date(time).toISOString().slice(0, 19) === value.slice(0, 19)
      ? time
      : NaN;
  }
  function origin(value) {
    try {
      return ["http:", "https:"].includes(new URL(value).protocol);
    } catch {
      return false;
    }
  }
  const valid = (c) =>
    typeof c.id === "string" &&
    /^[a-z][a-z0-9-]{0,31}$/.test(c.id) &&
    text(c.name, 64) &&
    kinds.includes(c.kind) &&
    typeof c.enabled === "boolean" &&
    text(c.image, 256) &&
    !c.image.includes("@") &&
    (c.version === null ||
      (typeof c.version === "string" &&
        /^[A-Za-z0-9._+-]{1,128}$/.test(c.version))) &&
    c.health === `/health/${c.id}` &&
    (!Object.hasOwn(c, "url") || (text(c.url, 2048) && origin(c.url)));
  function feature(value, keys, check) {
    if (!object(value)) return undefined;
    // An unknown feature field is a contract bump, not a partial feature.
    if (!only(value, keys)) throw new Error("Unknown feature field");
    return keys.every((k) => Object.hasOwn(value, k)) && check(value)
      ? value
      : undefined;
  }
  function parse(text, stack) {
    if (new TextEncoder().encode(text).length > MAX_BYTES)
      throw new Error("Status too large");
    const d = JSON.parse(text);
    if (
      !object(d) ||
      d.contract !== 2 ||
      !Object.hasOwn(ids, stack) ||
      d.stack !== stack ||
      !only(d, envelope) ||
      !envelope.every((k) => Object.hasOwn(d, k)) ||
      !Number.isFinite(timestamp(d.configuredAt)) ||
      !Array.isArray(d.components) ||
      d.components.length > 32 ||
      !object(d.features) ||
      !only(d.features, ["backups", "alerts"])
    )
      throw new Error("Unsupported status");
    const seen = new Set(),
      components = Object.create(null);
    for (const c of d.components) {
      if (!object(c)) continue;
      if (!only(c, fields)) throw new Error("Unknown component field");
      if (typeof c.id === "string") {
        if (seen.has(c.id)) throw new Error("Duplicate component");
        seen.add(c.id);
      }
      // Unknown IDs are ignored; invalid components render unknown.
      if (!valid(c) || !ids[stack].includes(c.id)) continue;
      components[c.id] = { enabled: c.enabled, version: c.version };
    }
    const backups = feature(
      d.features.backups,
      ["configured", "lastCheckpointAt"],
      (f) =>
        typeof f.configured === "boolean" &&
        (f.lastCheckpointAt === null ||
          Number.isFinite(timestamp(f.lastCheckpointAt))),
    );
    const alerts = feature(
      d.features.alerts,
      ["configured"],
      (f) => typeof f.configured === "boolean",
    );
    return {
      configuredAt: timestamp(d.configuredAt),
      components,
      features: {
        backups: backups && {
          configured: backups.configured,
          lastCheckpointAt:
            backups.lastCheckpointAt === null
              ? null
              : timestamp(backups.lastCheckpointAt),
        },
        alerts: alerts && { configured: alerts.configured },
      },
    };
  }
  // Everything here is configuration; liveness belongs to health paths.
  function view(doc, id) {
    const c = doc?.components[id];
    return {
      state: !c ? "unknown" : c.enabled ? "configured" : "off",
      version: c?.version ?? null,
      reason: !doc
        ? "Status unavailable"
        : !c
          ? "Not in status document"
          : `Configuration ${new Date(doc.configuredAt).toISOString()}`,
    };
  }
  function features(doc) {
    const { backups, alerts } = doc?.features || {},
      parts = [];
    if (backups)
      parts.push(
        !backups.configured
          ? "Backups not configured"
          : backups.lastCheckpointAt === null
            ? "Backups configured · no checkpoint recorded"
            : `Backups configured · last checkpoint ${new Date(backups.lastCheckpointAt).toISOString()}`,
      );
    if (alerts)
      parts.push(alerts.configured ? "Alerts configured" : "Alerts not configured");
    return parts.join(" · ");
  }
  async function request(url, fetcher = fetch) {
    const controller = new AbortController();
    let reader, timer;
    // Race the complete read, not just response headers. Abort also releases the socket.
    const deadline = new Promise((_, reject) => {
      timer = setTimeout(() => {
        controller.abort();
        reject(new Error("Request deadline"));
      }, DEADLINE_MS);
    });
    try {
      return await Promise.race([
        deadline,
        (async () => {
          const response = await fetcher(url, {
            credentials: "omit",
            cache: "no-store",
            redirect: "error",
            signal: controller.signal,
          });
          if (
            response.status !== 200 ||
            !/^application\/json(?:\s*;|\s*$)/i.test(
              response.headers.get("Content-Type") || "",
            )
          )
            throw new Error("Metadata unavailable");
          if (Number(response.headers.get("Content-Length")) > MAX_BYTES)
            throw new Error("Status too large");
          reader = response.body.getReader();
          const chunks = [];
          let size = 0;
          while (true) {
            const { value, done } = await reader.read();
            if (done) break;
            size += value.byteLength;
            if (size > MAX_BYTES) throw new Error("Status too large");
            chunks.push(value);
          }
          const bytes = new Uint8Array(size);
          let offset = 0;
          for (const chunk of chunks) {
            bytes.set(chunk, offset);
            offset += chunk.length;
          }
          return {
            text: new TextDecoder("utf-8", { fatal: true }).decode(bytes),
            date: response.headers.get("Date"),
          };
        })(),
      ]);
    } finally {
      clearTimeout(timer);
      controller.abort();
      if (reader) void reader.cancel().catch(() => {});
    }
  }
  async function pool(jobs, concurrency = 3) {
    let next = 0;
    await Promise.all(
      Array.from({ length: Math.min(concurrency, jobs.length) }, async () => {
        while (next < jobs.length) {
          const job = jobs[next++];
          try {
            await job();
          } catch {
            /* Independent metadata. */
          }
        }
      }),
    );
  }
  return { ids, parse, view, features, request, pool };
})();
if (typeof module !== "undefined") module.exports = StackStatus;
