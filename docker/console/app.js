const $ = (s) => document.querySelector(s),
  esc = (s) =>
    String(s ?? "").replace(
      /[&<>"']/g,
      (c) =>
        ({
          "&": "&amp;",
          "<": "&lt;",
          ">": "&gt;",
          '"': "&quot;",
          "'": "&#39;",
        })[c],
    );
let view =
  new URLSearchParams(location.search).get("view") === "map"
    ? "map"
    : "projects";
let overview = new URLSearchParams(location.search).get("project") || "all";
if (!DATA.projects.some((p) => p.id === overview)) overview = "all";
let project = overview,
  selected = "edge",
  query = "",
  showApplications = true,
  showLogging = true;
let detailOpen = false,
  checking = false,
  checked = "",
  notice = "",
  config;
const health = {};
const statusDocuments = {};
DATA.projects.sort((a, b) => (a.id === "edge" ? -1 : b.id === "edge" ? 1 : 0));
const github = (p) =>
  `<a class="github-link" href="https://github.com/${p.repo.includes("/") ? p.repo : "autonomiceng/" + p.repo}" target="_blank" rel="noreferrer" aria-label="${esc(p.name)} on GitHub"><img src="${DATA.icons.github}" alt=""> GitHub ↗</a>`;
const serviceRepos = {
  Caddy: "caddyserver/caddy",
  LiteLLM: "BerriAI/litellm",
  Langfuse: "langfuse/langfuse",
  "Langfuse worker": "langfuse/langfuse",
  RustFS: "rustfs/rustfs",
  PostgreSQL: "postgres/postgres",
  ClickHouse: "ClickHouse/ClickHouse",
  Valkey: "valkey-io/valkey",
  workerd: "cloudflare/workerd",
  Grafana: "grafana/grafana",
  Loki: "grafana/loki",
  Mimir: "grafana/mimir",
  Tempo: "grafana/tempo",
  Alloy: "grafana/alloy",
  "Postgres exporter": "prometheus-community/postgres_exporter",
  "Valkey exporter": "oliver006/redis_exporter",
};
const qualified = (s) =>
  DATA.projects.find((p) => p.id === s.project).name + " / " + s.name;
const matches = (s) =>
  `${s.name} ${s.description} ${qualified(s)}`
    .toLowerCase()
    .includes(query.toLowerCase());
const icon = (s) =>
  `<img class="logo" src="${DATA.icons[s.icon] || DATA.icons.gateway}" alt="">`;
function services() {
  return DATA.services.map((s) => ({
    ...s,
    links: serviceLinks(s),
    state: serviceState(s),
    version: serviceVersion(s),
    evidence: serviceEvidence(s),
  }));
}
const state = (s) =>
  ({
    off: "Not enabled",
    configured: "Configured",
    unknown: "Unknown",
  })[s.state] || s.state;
const badge = (s) =>
  `<span class="badge ${s.state.replaceAll(" ", "-")}"><i class="dot"></i>${esc(state(s))}</span>`;
function endpoints(s) {
  return Object.entries(s.links)
    .filter(([k]) => k !== "Console")
    .map(
      ([k, v]) =>
        `<div class="endpoint"><span>${esc(k)}</span><code>${esc(v)}</code><button class="copy" data-copy="${esc(v)}" aria-label="Copy ${esc(s.name)} ${esc(k)}"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true"><rect x="8" y="8" width="12" height="13" rx="2"/><path d="M15 8V3H3v13h5"/></svg></button></div>`,
    )
    .join("");
}
function title(s) {
  return s.links.Console
    ? `<a href="${esc(s.links.Console)}" target="_blank" rel="noreferrer">${esc(s.name)} ↗</a>`
    : esc(s.name);
}
function appCard(s) {
  return `<article class="app-card"><div class="service-title">${icon(s)}<h3>${title(s)}</h3>${badge(s)}</div><p class="description">${esc(s.description)}</p>${endpoints(s)}<small class="evidence">${esc(s.evidence)}</small><div class="card-bottom"><span class="version">${esc(s.version || "Version unavailable")}</span><button class="text-button inspect" data-select="${s.id}">Details →</button></div></article>`;
}
function row(s) {
  return `<div class="service-row">${icon(s)}<div class="grow"><h3><button class="text-button inspect" data-select="${s.id}">${esc(s.name)} →</button></h3><small>${esc(s.description)}</small><small>${esc(s.version || "")}</small><small class="evidence">${esc(s.evidence)}</small></div>${badge(s)}</div>`;
}
function detail() {
  const s = services().find((x) => x.id === selected);
  if (!s) return '<aside class="detail">Select a service.</aside>';
  const p = DATA.projects.find((p) => p.id === s.project);
  return `<aside class="detail">${view === "projects" ? '<button class="close-detail" aria-label="Close service details">×</button>' : ""}${icon(s)}<div class="meta">${esc(p.name)}</div><h2>${title(s)}</h2>${badge(s)}<p>${esc(s.description)}</p>${endpoints(s)}${!Object.keys(s.links).length ? '<div class="meta">Internal service · no browser endpoint</div>' : ""}<div class="version">${esc(s.version || "Version unavailable")}</div><p class="evidence">${esc(s.evidence)}</p><hr><h3>${s.id === "alloy" ? "Sends telemetry to" : "Connects to"}</h3>${
    s.uses.length
      ? s.uses
          .map((id) => {
            const t = services().find((x) => x.id === id);
            return t
              ? `<button class="related text-button" data-select="${id}">${esc(qualified(t))} →</button>`
              : "";
          })
          .join("")
      : "<p>No downstream connections shown.</p>"
  }${s.kind !== "storage" && s.kind !== "capability" && s.kind !== "setup" ? "<hr><h3>Logs</h3><p>stdout / stderr → host journal<br>With Observability: Alloy → Loki</p>" : ""}${s.id === "edge" ? "<p>Edge routes through each project’s Caddy.</p>" : ""}<hr>${github({ name: s.name, repo: serviceRepos[s.name] || p.repo })}</aside>`;
}
function ProjectCards() {
  return `<div class="projects">${
    DATA.projects
      .filter(
        (p) =>
          (overview === "all" || p.id === overview) &&
          services().some((s) => s.project === p.id && matches(s)),
      )
      .map((p) => {
        const all = services().filter((s) => s.project === p.id && matches(s)),
          apps = all.filter((s) => s.kind === "app" || s.id === "edge"),
          rest = all.filter(
            (s) => s.kind !== "app" && s.kind !== "setup" && s.id !== "edge",
          ),
          jobs = all.filter((s) => s.kind === "setup"),
          active = all.filter((s) => s.state === "configured").length,
          features = StackStatus.features(statusDocuments[p.id]);
        return `<section class="project"><div class="project-top">${icon(p)}<div class="grow"><h2>${esc(p.name)}</h2><p>${esc(p.description)}</p><span class="meta">${active} configured · ${all.length - jobs.length} ${all.length - jobs.length === 1 ? "service" : "services"}</span>${features ? `<div class="meta features">${esc(features)}</div>` : ""}</div>${github(p)}</div><div class="apps">${apps.map(appCard).join("")}</div>${
          rest.length
            ? `<details data-key="${p.id}-services" ${query || overview !== "all" ? "open" : ""}><summary>All services<small>${rest.length} supporting components</small></summary><div class="inventory">${[
                "infra",
                "backend",
                "storage",
                "capability",
                "worker",
              ]
                .map((k) => {
                  const group = rest.filter((s) => s.kind === k);
                  return group.length
                    ? `<div class="section-label">${{ backend: "Data", storage: "Files", capability: "Capabilities", worker: "Workers & collection", infra: "Routing", setup: "One-time setup jobs" }[k]}</div>${group.map(row).join("")}`
                    : "";
                })
                .join("")}</div></details>`
            : ""
        }${jobs.length ? `<details class="installation-details" data-key="${p.id}-installation"><summary>Installation details</summary><div class="inventory">${jobs.map(row).join("")}</div></details>` : ""}${overview === "all" ? `<div class="project-footer"><a href="?project=${p.id}">Project overview →</a></div>` : ""}</section>`;
      })
      .join("") || '<p class="empty">No matching services.</p>'
  }</div>`;
}
function render() {
  const open = [...document.querySelectorAll("details[open]")].map(
    (el) => el.dataset.key,
  );
  const focused = document.activeElement;
  const focusIdentity = focused
    ? {
        id: focused.id,
        view: focused.dataset.view,
        project: focused.dataset.project,
        label: focused.getAttribute("aria-label"),
        select: focused.dataset.select,
        href: focused.getAttribute("href"),
        dialog: Boolean(focused.closest(".detail-drawer")),
      }
    : null;
  const isSearch = focused?.matches(".search");
  const cursor = isSearch ? focused.selectionStart : null;
  $("#app").innerHTML =
    `<div class="toolbar">${overview !== "all" ? '<a class="back-link" href="/">← All projects</a>' : ""}<nav class="view-tabs" aria-label="View"><button data-view="projects" class="${view === "projects" ? "active" : ""}">Projects</button><button data-view="map" class="${view === "map" ? "active" : ""}">Map</button></nav>${view === "projects" ? `<input class="search" aria-label="Find a service" placeholder="Search projects and services…" value="${esc(query)}">` : ""}<button id="refresh" ${checking ? "disabled" : ""}>${checking ? "Checking…" : "Refresh"}</button><span id="checked" role="status">${esc(checked)}</span></div><div class="notice">${esc(notice)}</div>${view === "projects" ? ProjectCards() : AllProjectsMap()}${view === "projects" && detailOpen ? `<div class="detail-shade"></div><div class="detail-drawer" role="dialog" aria-modal="true" aria-label="Service details">${detail()}</div>` : ""}`;
  if (view === "projects") {
    const grid = $(".projects"),
      cards = [...grid.children];
    if (overview !== "all") grid.classList.add("focused-project");
    grid.innerHTML =
      '<div class="project-column"></div><div class="project-column"></div>';
    cards.forEach((card, i) =>
      grid.children[overview === "all" ? i % 2 : 0].append(card),
    );
  }
  for (const el of document.querySelectorAll("details"))
    if (open.includes(el.dataset.key)) el.open = true;
  document.querySelector("header").inert = detailOpen && view === "projects";
  for (const element of document.querySelectorAll(
    "#app > .toolbar, #app > .projects",
  ))
    element.inert = detailOpen && view === "projects";
  if (!isSearch && focusIdentity) {
    const scope = focusIdentity.dialog
      ? document.querySelector(".detail-drawer")
      : document;
    const candidate = [
      ...(scope?.querySelectorAll("button,a,input") || []),
    ].find(
      (el) =>
        (focusIdentity.id && el.id === focusIdentity.id) ||
        (focusIdentity.view && el.dataset.view === focusIdentity.view) ||
        (focusIdentity.project &&
          el.dataset.project === focusIdentity.project) ||
        (focusIdentity.label &&
          el.getAttribute("aria-label") === focusIdentity.label) ||
        (focusIdentity.select && el.dataset.select === focusIdentity.select) ||
        (focusIdentity.href && el.getAttribute("href") === focusIdentity.href),
    );
    candidate?.focus();
  }
  if (isSearch && $(".search")) {
    $(".search").focus();
    $(".search").setSelectionRange(cursor, cursor);
  }
}
function switchView(value) {
  view = value;
  const url = new URL(location);
  url.searchParams.set("view", view);
  history.replaceState({}, "", url);
  render();
}
function AllProjectsMap() {
  const all = services().filter((s) => s.kind !== "setup");
  const list = all.filter(
    (s) =>
      project === "all" ||
      s.project === project ||
      (showLogging && s.id === "alloy"),
  );
  const groups = DATA.projects.filter((p) =>
    list.some((s) => s.project === p.id),
  );
  const positions = {},
    width = Math.max(600, groups.length * 298);
  let maxRows = 0;
  groups.forEach((p, i) => {
    const group = list
      .filter((s) => s.project === p.id)
      .sort((a, b) => (a.name === "Caddy" ? -1 : b.name === "Caddy" ? 1 : 0));
    maxRows = Math.max(maxRows, group.length);
    group.forEach(
      (s, j) => (positions[s.id] = { x: 22 + i * 298, y: 70 + j * 92 }),
    );
  });
  const height = maxRows * 92 + 95;
  const links = [];
  for (const s of list) {
    for (const id of s.uses)
      if (
        positions[id] &&
        showApplications &&
        !(s.id === "alloy" && id === "loki")
      )
        links.push({ from: s.id, to: id, type: "app" });
    if (showLogging && s.kind !== "storage" && s.kind !== "capability" && s.id !== "alloy" && !s.optional)
      links.push({ from: s.id, to: "alloy", type: "logs" });
  }
  if (showLogging && positions.alloy && positions.loki)
    links.push({ from: "alloy", to: "loki", type: "logs" });
  const connected = new Set([
    selected,
    ...links
      .filter((e) => e.from === selected || e.to === selected)
      .flatMap((e) => [e.from, e.to]),
  ]);
  const edges = links
    .map((e) => {
      const a = positions[e.from],
        b = positions[e.to],
        same = a.x === b.x;
      const x1 = a.x + 252,
        y1 = a.y + 31,
        x2 = same ? b.x + 252 : b.x,
        y2 = b.y + 31;
      const path = same
        ? `M${x1},${y1} C${x1 + 22},${y1} ${x1 + 22},${y2} ${x2},${y2}`
        : `M${x1},${y1} C${(x1 + x2) / 2},${y1} ${(x1 + x2) / 2},${y2} ${x2},${y2}`;
      return `<path class="edge ${e.type} ${e.from === selected || e.to === selected ? "highlight" : ""}" d="${path}" marker-end="url(#all-arrow)"><title>${esc(qualified(list.find((s) => s.id === e.from)))} → ${esc(qualified(list.find((s) => s.id === e.to)))}${e.type === "logs" ? " · logs collected through Docker" : ""}</title></path>`;
    })
    .join("");
  return `<nav class="tabs" aria-label="Projects"><button data-project="all" class="${project === "all" ? "active" : ""}">All projects</button>${DATA.projects.map((p) => `<button data-project="${p.id}" class="${project === p.id ? "active" : ""}">${esc(p.name)}</button>`).join("")}</nav><div class="connection-toggles"><label><input type="checkbox" id="application-connections" ${showApplications ? "checked" : ""}> Application connections</label><label><input type="checkbox" id="logging-connections" ${showLogging ? "checked" : ""}> Log collection (when configured)</label><button class="text-button meta" id="clear-map">Clear selection</button></div><div class="map-layout"><div><div class="map-wrap"><svg class="map all-map" viewBox="0 0 ${width} ${height}" role="group" aria-label="All project connections"><defs><marker id="all-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="4" markerHeight="4" orient="auto"><path d="M0 0 L10 5 L0 10" fill="#9faad7"/></marker></defs>${groups.map((p, i) => `<rect class="lane" x="${12 + i * 298}" y="8" width="276" height="${height - 20}" rx="12"/><text class="project-label" x="${25 + i * 298}" y="38">${esc(p.name)}</text>`).join("")}${edges}${list
    .map((s) => {
      const { x, y } = positions[s.id];
      return `<g class="node ${selected === s.id ? "selected" : ""} ${selected && !connected.has(s.id) ? "dim" : ""} ${s.optional ? "optional" : ""}" tabindex="0" role="button" aria-label="Inspect ${esc(qualified(s))}" data-select="${s.id}"><rect x="${x}" y="${y}" width="252" height="68" rx="9"/><image href="${DATA.icons[s.icon] || DATA.icons.gateway}" x="${x + 12}" y="${y + 16}" width="22" height="22"/><text x="${x + 44}" y="${y + 27}">${esc(s.name)}</text><text class="state-label" x="${x + 44}" y="${y + 48}">${esc(state(s))}</text></g>`;
    })
    .join("")}</svg></div></div>${detail()}</div>`;
}

const probes = {
  edge: "",
  lite: "litellm",
  langfuse: "langfuse",
  "g-rust": "s3",
  bp: "backplane",
  grafana: "observability",
};
const statusIds = {
  edge: "caddy",
  lite: "litellm",
  langfuse: "langfuse-web",
  "lf-worker": "langfuse-worker",
  "g-rust": "rustfs",
  "g-pg": "postgres",
  "g-caddy": "caddy",
  "pg-export": "postgres-exporter",
  "vk-export": "valkey-exporter",
  bp: "server",
  "b-pg": "postgres",
  "b-rust": "rustfs",
  "b-caddy": "caddy",
  "o-caddy": "caddy",
  "o-rust": "rustfs",
};
const statusKey = (s) => statusIds[s.id] || s.id;
function observation(s) {
  return StackStatus.view(statusDocuments[s.project], statusKey(s));
}
function serviceEvidence(s) {
  const parts = [observation(s).reason];
  if (probes[s.id] !== undefined)
    parts.push(`HTTP reachability: ${health[probes[s.id]] || "checking"}`);
  return parts.join(" · ");
}
function validateConfig(value) {
  const hostname = /^(?=.{1,253}$)[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$/i;
  if (
    !value ||
    typeof value !== "object" ||
    typeof value.domain !== "string" ||
    !hostname.test(value.domain) ||
    typeof value.tailnet !== "string" ||
    (value.tailnet && !hostname.test(value.tailnet)) ||
    typeof value.root !== "string" ||
    (value.root && !/^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/i.test(value.root)) ||
    !["http", "https"].includes(value.scheme)
  )
    throw new Error("Invalid access settings");
  return {
    ...value,
    domain: value.domain.toLowerCase(),
    tailnet: value.tailnet.toLowerCase(),
    root: (value.root || "platform").toLowerCase(),
  };
}
// The console's own Tailnet Origin.
function tailnetHost() {
  return config?.tailnet ? `${config.root}.${config.tailnet}` : "";
}
// Links are trusted only on the console's own addresses; with a tailnet recorded, the Tailnet
// Origins are the applications' browser URLs wherever the page was opened.
function appOrigin(id, prefix) {
  if (!config) return null;
  if (
    location.hostname !== tailnetHost() &&
    location.hostname !== config.domain &&
    !(config.domain === "localhost" && location.hostname === "127.0.0.1")
  )
    return null;
  if (config.tailnet) return `https://${prefix || config.root}.${config.tailnet}`;
  return `${location.protocol}//${prefix ? prefix + "." : ""}${config.domain}${location.port ? ":" + location.port : ""}`;
}
function serviceState(s) {
  return observation(s).state;
}
function serviceVersion(s) {
  const { version } = observation(s);
  return version ? `Configured ${version}` : "";
}
function serviceLinks(s) {
  const links = {},
    add = (key, id, prefix, path = "") => {
      const origin = appOrigin(id, prefix);
      if (origin) links[key] = origin + path;
    };
  if (s.id === "lite") {
    add("Console", "litellm", "litellm", "/ui/");
    add("API", "litellm", "litellm");
  }
  if (s.id === "langfuse") {
    add("Console", "langfuse", "langfuse", "/");
    add("OTLP API", "langfuse", "langfuse", "/api/public/otel");
  }
  if (s.id === "g-rust") {
    add("Console", "rustfs", "rustfs", "/rustfs/console/");
    add("S3 API", "s3", "s3");
  }
  if (s.id === "bp") {
    add("Console", "backplane", "backplane", "/dashboard/");
    add("API", "backplane", "backplane", "/api/v1");
  }
  if (s.id === "grafana") add("Console", "observability", "grafana", "/");
  return links;
}
function accessNotice() {
  if (!config)
    return "Application addresses are unavailable. Refresh to try again.";
  if (
    location.hostname !== tailnetHost() &&
    location.hostname !== config.domain &&
    !(config.domain === "localhost" && location.hostname === "127.0.0.1")
  )
    return "Application access is not configured for this address. Use the configured domain, or run bootstrap --tailscale on the host and open the console's Tailnet address.";
  return "";
}
async function check() {
  if (checking) return;
  checking = true;
  render();
  let settingsOK = true;
  try {
    const jobs = Object.keys(StackStatus.ids).map((stack) => async () => {
      try {
        const result = await StackStatus.request(`/stack-status/${stack}`);
        statusDocuments[stack] = StackStatus.parse(result.text, stack);
      } catch {
        // An absent or invalid document is unknown, never a previous answer.
        delete statusDocuments[stack];
      }
      render();
    });
    jobs.push(
      async () => {
        try {
          const result = await StackStatus.request("/edge-config.json");
          config = validateConfig(JSON.parse(result.text));
        } catch {
          settingsOK = false;
        }
        render();
      },
    );
    for (const id of new Set([...Object.values(probes), "rustfs"]))
      jobs.push(async () => {
        try {
          const response = await fetch("/health" + (id ? "/" + id : ""), {
            method: "GET",
            cache: "no-store",
            credentials: "omit",
            redirect: "error",
            signal: AbortSignal.timeout(4000),
          });
          health[id] = response.status === 200 ? "reachable" : "unavailable";
        } catch {
          health[id] = "unavailable";
        }
        render();
      });
    await StackStatus.pool(jobs);
    $("#host-name").textContent = location.hostname;
    $("#access-mode").textContent =
      tailnetHost() && location.hostname === tailnetHost()
        ? "Tailscale"
        : location.protocol === "https:"
          ? "HTTPS"
          : "Local HTTP";
    checked =
      (settingsOK ? "Updated " : "Addresses could not refresh · checked ") +
      new Date().toLocaleTimeString();
    notice = accessNotice();
  } finally {
    checking = false;
    render();
  }
}
let detailTrigger = null;
function closeDetail() {
  detailOpen = false;
  render();
  const target = [...document.querySelectorAll("[data-select]")].find(
    (el) => el.dataset.select === detailTrigger,
  );
  target?.focus();
}
document.addEventListener("click", async (e) => {
  const copy = e.target.closest("[data-copy]");
  if (copy) {
    try {
      await navigator.clipboard.writeText(copy.dataset.copy);
      $("#toast").textContent = "Endpoint copied";
    } catch {
      $("#toast").textContent = "Select the endpoint text to copy.";
    }
    setTimeout(() => ($("#toast").textContent = ""), 2500);
    return;
  }
  const tab = e.target.closest("[data-view]");
  if (tab) {
    switchView(tab.dataset.view);
    return;
  }
  if (e.target.closest("#refresh")) {
    check();
    return;
  }
  if (e.target.closest(".close-detail,.detail-shade")) {
    closeDetail();
    return;
  }
  const group = e.target.closest("[data-project]");
  if (group) {
    project = group.dataset.project;
    selected = DATA.projects.find((p) => p.id === project)?.primary || "edge";
    render();
    return;
  }
  const node = e.target.closest("[data-select]");
  if (node) {
    selected = node.dataset.select;
    if (view === "projects") {
      detailTrigger = selected;
      detailOpen = true;
    }
    render();
    if (detailOpen) $(".close-detail")?.focus();
    return;
  }
  if (e.target.closest("#clear-map")) {
    selected = "";
    render();
    $("#clear-map")?.focus();
  }
});
document.addEventListener("input", (e) => {
  if (e.target.matches(".search")) {
    query = e.target.value;
    render();
  }
});
document.addEventListener("change", (e) => {
  if (e.target.id === "application-connections") {
    showApplications = e.target.checked;
    render();
    $("#application-connections").focus();
  }
  if (e.target.id === "logging-connections") {
    showLogging = e.target.checked;
    render();
    $("#logging-connections").focus();
  }
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && detailOpen) {
    closeDetail();
    return;
  }
  if ((e.key === "Enter" || e.key === " ") && e.target.matches(".node")) {
    e.preventDefault();
    e.target.dispatchEvent(new MouseEvent("click", { bubbles: true }));
  }
  if (e.key === "Tab" && detailOpen) {
    const nodes = [
      ...document.querySelectorAll(
        ".detail-drawer a[href],.detail-drawer button",
      ),
    ];
    if (!nodes.length) return;
    const first = nodes[0],
      last = nodes.at(-1);
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault();
      first.focus();
    }
  }
});
try {
  config = validateConfig(JSON.parse($("#edge-config").textContent));
} catch {
  config = null;
}
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) check();
});
setInterval(() => {
  if (!document.hidden) check();
}, 30000);
check();
