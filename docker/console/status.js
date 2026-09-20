// Public status v1. Keep parsing, evidence age and transport independent of the DOM.
const StackStatus = (() => {
  const MAX_BYTES = 65536;
  const DEADLINE_MS = 4000;
  const ids = {
    edge: ["caddy", "bootstrap"],
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
      "bootstrap",
      "rustfs-init",
    ],
    backplane: [
      "server",
      "postgres",
      "caddy",
      "rustfs",
      "workerd",
      "files",
      "functions",
      "bootstrap",
      "migrate",
      "data-init",
      "blob-bootstrap",
    ],
    observability: [
      "caddy",
      "grafana",
      "alloy",
      "loki",
      "mimir",
      "tempo",
      "rustfs",
      "bootstrap",
      "rustfs-init",
    ],
  };
  const tasks = new Set([
    "bootstrap",
    "rustfs-init",
    "migrate",
    "data-init",
    "blob-bootstrap",
  ]);
  const states = [
    "healthy",
    "degraded",
    "starting",
    "unavailable",
    "disabled",
    "absent",
    "unknown",
  ];
  const version = (value) =>
    typeof value === "string" && /^[A-Za-z0-9._+-]{1,128}$/.test(value)
      ? value
      : null;
  const digest = (value) =>
    typeof value === "string" && /^sha256:[0-9a-f]{64}$/.test(value)
      ? value
      : null;
  const object = (value) =>
    value !== null && typeof value === "object" && !Array.isArray(value);
  const ttl = (value) => Number.isInteger(value) && value >= 1 && value <= 300;
  function timestamp(value) {
    if (value === null) return null;
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
  const validTime = (time, now, generated) =>
    time === null ||
    (Number.isFinite(time) && time <= now + 5000 && time <= generated + 5000);
  const fresh = (time, seconds, now) =>
    time !== null && time <= now + 5000 && now - time <= seconds * 1000;
  function parse(text, stack, now = Date.now(), date = null) {
    if (new TextEncoder().encode(text).length > MAX_BYTES)
      throw new Error("Status too large");
    const d = JSON.parse(text);
    if (
      !object(d) ||
      d.schemaVersion !== 1 ||
      !Object.hasOwn(ids, stack) ||
      d.stack !== stack ||
      !Array.isArray(d.components) ||
      d.components.length > 32
    )
      throw new Error("Unsupported status");
    const generated = timestamp(d.generatedAt),
      configuration = timestamp(d.configurationObservedAt);
    if (
      !Number.isFinite(generated) ||
      !ttl(d.configurationValidForSeconds) ||
      !["configured", "disabled", "unknown"].includes(d.telemetry) ||
      !validTime(configuration, generated, generated) ||
      (configuration === null && d.telemetry !== "unknown")
    )
      throw new Error("Invalid status envelope");
    // An invalid supplied Date cannot silently fall back to generatedAt.
    const serverClock =
      date === null
        ? generated
        : /^(Mon|Tue|Wed|Thu|Fri|Sat|Sun), \d{2} [A-Z][a-z]{2} \d{4} \d{2}:\d{2}:\d{2} GMT$/.test(
              date,
            )
          ? Date.parse(date)
          : NaN;
    const clockAgrees =
      Number.isFinite(serverClock) &&
      (date === null || new Date(serverClock).toUTCString() === date) &&
      Math.abs(now - serverClock) <= 5000;
    if (clockAgrees && !validTime(configuration, now, generated))
      throw new Error("Invalid configuration time");
    if (clockAgrees && generated > now + 5000)
      throw new Error("Invalid generation time");
    const seen = new Set(),
      components = Object.create(null);
    for (const c of d.components) {
      if (object(c) && typeof c.id === "string") {
        if (seen.has(c.id)) throw new Error("Duplicate component");
        seen.add(c.id);
      }
      if (!object(c) || !ids[stack].includes(c.id)) continue;
      const kind = tasks.has(c.id)
        ? "task"
        : ["files", "functions"].includes(c.id)
          ? "capability"
          : "service";
      const observed = timestamp(c.observedAt),
        execution = kind === "task" ? timestamp(c.lastExecutionAt) : null;
      if (
        c.kind !== kind ||
        ![true, false, null].includes(c.configured) ||
        !states.includes(c.state) ||
        !ttl(c.validForSeconds) ||
        !validTime(observed, now, generated) ||
        !validTime(execution, now, generated)
      )
        continue;
      if (c.state !== "unknown" && observed === null) continue;
      if (!["unknown", "disabled"].includes(c.state) && c.configured !== true)
        continue;
      if (
        c.state === "disabled" &&
        (c.configured !== false || observed !== configuration)
      )
        continue;
      if (
        kind === "task" &&
        execution === null &&
        !["unknown", "disabled"].includes(c.state)
      )
        continue;
      if (
        configuration === null &&
        (c.configured !== null ||
          version(c.configuredVersion) !== null ||
          digest(c.configuredDigest) !== null)
      )
        continue;
      components[c.id] = {
        kind,
        state: c.state,
        configured: c.configured,
        observed,
        execution,
        validForSeconds: c.validForSeconds,
        configuredVersion: version(c.configuredVersion),
        observedVersion: version(c.observedVersion),
        configuredDigest: digest(c.configuredDigest),
        observedImageId: digest(c.observedImageId),
      };
    }
    return {
      generated,
      configuration,
      configurationValidForSeconds: d.configurationValidForSeconds,
      telemetry: d.telemetry,
      clockAgrees,
      components,
    };
  }
  function configurationFresh(doc, now, failed) {
    return Boolean(doc?.clockAgrees && !failed && fresh(doc.configuration, doc.configurationValidForSeconds, now));
  }
  function telemetry(doc, now = Date.now(), failed = false) {
    return configurationFresh(doc, now, failed) ? doc.telemetry : "unknown";
  }
  function view(doc, id, now = Date.now(), failed = false) {
    const c = doc?.components[id];
    const configFresh = configurationFresh(doc, now, failed);
    const observedFresh = Boolean(
      c &&
        doc.clockAgrees &&
        !failed &&
        fresh(c.observed, c.validForSeconds, now),
    );
    const current = observedFresh && (c.state !== "disabled" || configFresh);
    const reason = !doc
      ? "Metadata unavailable"
      : !doc.clockAgrees
        ? "Clock disagreement"
        : failed
          ? c && c.observed !== null
            ? "Metadata unavailable · stale"
            : "Metadata unavailable"
          : c && c.observed !== null && !current
            ? "Stale observation"
            : !c || c.observed === null
              ? "Unknown observation"
              : "";
    return {
      state: current ? c.state : "unknown",
      reason,
      kind: c?.kind,
      observedAt: c?.observed ?? null,
      lastExecutionAt: c?.execution ?? null,
      configured: configFresh ? (c?.configured ?? null) : null,
      configurationAt: doc?.configuration ?? null,
      configFresh,
      telemetry: configFresh ? doc.telemetry : "unknown",
      configuredVersion: configFresh ? (c?.configuredVersion ?? null) : null,
      configuredDigest: configFresh ? (c?.configuredDigest ?? null) : null,
      observedVersion: observedFresh ? c.observedVersion : null,
      observedImageId: observedFresh ? c.observedImageId : null,
    };
  }
  function legacy(data, id, now = Date.now()) {
    if (typeof id !== "string") return null;
    const value = version(object(data?.images) ? data.images[id] : null);
    if (!value) return null;
    const supplied = data.configuredAt ?? data.pinnedAt;
    const time = supplied == null ? null : timestamp(supplied);
    if (supplied != null && (!Number.isFinite(time) || time > now + 5000))
      return null;
    return `Configured ${value} · ${time === null ? "undated" : new Date(time).toISOString()}`;
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
  return { ids, parse, view, telemetry, legacy, request, pool };
})();
if (typeof module !== "undefined") module.exports = StackStatus;
