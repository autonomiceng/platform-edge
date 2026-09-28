const $ = (s) => document.querySelector(s);
const esc = (s) =>
  String(s ?? "").replace(
    /[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c],
  );
const LABELS = {
  healthy: "Healthy",
  degraded: "Degraded",
  unreachable: "Unreachable",
  unknown: "Unknown",
  disabled: "Disabled",
  configured: "Configured",
};
const COPY_ICON =
  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><rect x="8" y="8" width="12" height="13" rx="2"/><path d="M15 8V3H3v13h5"/></svg>';
const GITHUB_ICON =
  '<svg viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><path d="M6.766 11.328c-2.063-.25-3.516-1.734-3.516-3.656 0-.781.281-1.625.75-2.188-.203-.515-.172-1.609.063-2.062.625-.078 1.468.25 1.968.703.594-.187 1.219-.281 1.985-.281.765 0 1.39.094 1.953.265.484-.437 1.344-.765 1.969-.687.218.422.25 1.515.046 2.047.5.593.766 1.39.766 2.203 0 1.922-1.453 3.375-3.547 3.64.531.344.89 1.094.89 1.954v1.625c0 .468.391.734.86.547C13.781 14.359 16 11.53 16 8.03 16 3.61 12.406 0 7.984 0 3.563 0 0 3.61 0 8.031a7.88 7.88 0 0 0 5.172 7.422c.422.156.828-.125.828-.547v-1.25c-.219.094-.5.156-.75.156-1.031 0-1.64-.562-2.078-1.609-.172-.422-.36-.672-.719-.719-.187-.015-.25-.093-.25-.187 0-.188.313-.328.625-.328.453 0 .844.281 1.25.86.313.452.64.655 1.031.655s.641-.14 1-.5c.266-.265.47-.5.657-.656"/></svg>';
const health = {}; // Edge probe name -> { state, detail }
const documents = {}; // stack -> parsed Status Document
let config = null,
  checking = false,
  drawer = null, // { project, app?, trigger }
  checkedText = "";

const icon = (file) => `<img src="/console/icons/${file}" alt="">`;
const badge = (state) => `<span class="pk-badge" data-state="${state}">${LABELS[state]}</span>`;
// Break long URLs after "//" and before a "." or "/" that starts a segment, never inside a word.
const breakable = (value) =>
  esc(value).replace(/\/\/|[./](?=[\w-]{2})/g, (m) => (m === "//" ? "//<wbr>" : "<wbr>" + m));
const utc = (time) => new Date(time).toISOString().slice(0, 16).replace("T", " ") + " UTC";
const repoUrl = (repo) => `https://github.com/${repo.includes("/") ? repo : "autonomiceng/" + repo}`;
const external = (href, text, key) =>
  `<a href="${esc(href)}" target="_blank" rel="noreferrer" data-key="${key}">${text} ↗</a>`;
const find = (projectId, appId) => {
  const project = DATA.projects.find((p) => p.id === projectId);
  return { project, app: project?.apps.find((a) => a.id === appId) };
};
const component = (project, id) => StackStatus.view(documents[project.id], id);

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
    typeof value.apps !== "string" ||
    !/^[a-z0-9,]*$/.test(value.apps) ||
    !["http", "https"].includes(value.scheme)
  )
    throw new Error("Invalid access settings");
  return {
    ...value,
    domain: value.domain.toLowerCase(),
    tailnet: value.tailnet.toLowerCase(),
    root: (value.root || "platform").toLowerCase(),
    apps: value.apps.split(",").filter(Boolean),
  };
}
// The console's own Tailnet Origin.
function tailnetHost() {
  return config?.tailnet ? `${config.root}.${config.tailnet}` : "";
}
const trustedAddress = () =>
  location.hostname === tailnetHost() ||
  location.hostname === config.domain ||
  (config.domain === "localhost" && location.hostname === "127.0.0.1");
// Links are trusted only on the console's own addresses. With a tailnet recorded, the Tailnet
// Origin of a selected node is the application's browser URL wherever the page was opened; an
// application without a node keeps its public-domain link, which the Tailnet console cannot offer.
function appOrigin(prefix) {
  if (!config || !trustedAddress()) return null;
  if (config.tailnet && config.apps.includes(prefix || "console"))
    return `https://${prefix || config.root}.${config.tailnet}`;
  if (location.hostname === tailnetHost()) return null;
  return `${location.protocol}//${prefix ? prefix + "." : ""}${config.domain}${location.port ? ":" + location.port : ""}`;
}
// Browser URL first, then client endpoints; rows without a trusted origin are left out.
function endpoints(app) {
  const rows = [];
  for (const [label, prefix, path] of [["URL", ...app.open], ...app.endpoints]) {
    const origin = appOrigin(prefix);
    if (origin) rows.push([label, origin + path]);
  }
  return rows;
}
function accessNotice() {
  if (!config) return "Application addresses are unavailable. Refresh to try again.";
  if (!trustedAddress())
    return "Application access is not configured for this address. Use the configured domain, or run bootstrap --tailscale on the host and open the console's Tailnet address.";
  return "";
}
// Disabled comes from the Status Document; reachability only from Edge's Health Paths. Without
// a valid document entry the card stays Unknown, whatever its probe says.
function appState(project, app) {
  const { state } = component(project, app.status);
  if (state !== "configured") return state === "off" ? "disabled" : "unknown";
  const results = app.health.map((name) => health[name]?.state);
  const up = results.filter((r) => r === "healthy").length,
    down = results.filter((r) => r === "unreachable").length;
  if (up === results.length) return "healthy";
  if (down === results.length) return "unreachable";
  return up && down ? "degraded" : "unknown";
}
function versionText(project, id) {
  const { state, version } = component(project, id);
  if (state === "unknown") return "Version unknown";
  return version ? `Configured ${version}` : "Configured";
}
function reachable(project) {
  const apps = project.apps.filter((a) => appState(project, a) !== "disabled");
  const up = apps.filter((a) => ["healthy", "degraded"].includes(appState(project, a)));
  return [up.length, apps.length];
}

function endpointRows(app) {
  return endpoints(app)
    .map(
      ([label, value]) =>
        `<div class="pk-endpoint"><span>${label}</span><code>${breakable(value)}</code><button class="pk-copy" type="button" data-copy="${esc(value)}" data-key="copy-${app.id}-${label}" aria-label="Copy ${esc(app.name)} ${label}">${COPY_ICON}</button></div>`,
    )
    .join("");
}
function appCard(project, app) {
  const [, url] = endpoints(app).find(([label]) => label === "URL") || [];
  const title = url ? external(url, esc(app.name), `open-${app.id}`) : esc(app.name);
  const rows = endpointRows(app);
  return `<article class="pk-app"><div class="pk-app-head">${icon(app.icon)}<h3>${title}</h3>${badge(appState(project, app))}</div><p class="pk-app-desc">${esc(app.description)}</p>${rows ? `<div class="pk-endpoints">${rows}</div>` : ""}<div class="pk-app-foot"><span class="version">${versionText(project, app.status)}</span><button class="pk-button pk-plain" type="button" data-open="${project.id}/${app.id}" data-key="details-${project.id}-${app.id}">Details</button></div></article>`;
}
function projectSection(project) {
  const [up, total] = reachable(project);
  return `<section class="pk-card project${project.apps.length > 1 ? " wide" : ""}" data-project="${project.id}" aria-labelledby="project-${project.id}"><div class="project-head">${icon(project.icon)}<div><h2 id="project-${project.id}">${esc(project.name)}</h2><p>${esc(project.description)}</p><div class="project-meta"><span class="reachable">${up} of ${total} ${total === 1 ? "app" : "apps"} reachable</span><button class="pk-button pk-plain" type="button" data-open="${project.id}" data-key="details-${project.id}" aria-label="${esc(project.name)} details">Details</button></div></div>${external(repoUrl(project.repo), `${GITHUB_ICON}<span class="pk-sr-only">${esc(project.name)} on </span>GitHub`, `github-${project.id}`)}</div><div class="apps">${project.apps.map((a) => appCard(project, a)).join("")}</div></section>`;
}
function rows(items) {
  return `<div class="pk-endpoints">${items.map(([label, value]) => `<div class="pk-endpoint"><span>${label}</span><span>${value}</span></div>`).join("")}</div>`;
}
function appDrawer(project, app) {
  const doc = documents[project.id],
    c = doc?.components[app.status],
    state = appState(project, app),
    links = endpointRows(app);
  return `<div class="pk-drawer-head">${icon(app.icon)}<h2 id="drawer-title">${esc(app.name)}</h2>${badge(state)}<button class="pk-button pk-close" type="button" data-close data-key="close" aria-label="Close details">×</button></div><div class="pk-drawer-body"><p class="drawer-lead">${esc(app.description)} Part of ${esc(project.name)}.</p>${links ? `<section><h3 class="pk-section-label">Endpoints</h3><div class="pk-endpoints">${links}</div></section>` : ""}<section><h3 class="pk-section-label">Configuration</h3>${rows([
    ["Image", c ? `<code>${breakable(c.image)}</code>` : "Unknown"],
    ["Version", c ? esc(c.version || "Not reported") : "Unknown"],
    ["Configured at", doc ? utc(doc.configuredAt) : "Status unavailable"],
    ...app.health.map((name) => ["Health", `<code>/health/${name}</code> · ${health[name]?.detail || "not checked"}`]),
  ])}</section><section><h3 class="pk-section-label">Links</h3><div class="drawer-links">${external(repoUrl(app.source), "Source", "source")}${external(repoUrl(project.repo), `${esc(project.name)} on GitHub`, "project-github")}</div></section></div>`;
}
// A missing feature is unknown to the producer, so it gets no row.
function featureRows(doc) {
  const { backups, alerts } = doc?.features || {},
    out = [];
  if (backups)
    out.push(["Backups", !backups.configured ? "Not configured" : backups.lastCheckpointAt === null ? "Configured · no checkpoint recorded" : `Configured · last checkpoint ${utc(backups.lastCheckpointAt)}`]);
  if (alerts) out.push(["Alerts", alerts.configured ? "Configured" : "Not configured"]);
  return out;
}
function projectDrawer(project) {
  const doc = documents[project.id],
    [up, total] = reachable(project);
  const list = project.components
    .map((c) => {
      const { state, version } = component(project, c.id);
      return `<li>${icon(c.icon)}<div class="component"><strong>${esc(c.name)}</strong><span>${esc(c.description)}</span></div>${version ? `<code>${esc(version)}</code>` : ""}${badge(state === "off" ? "disabled" : state)}</li>`;
    })
    .join("");
  return `<div class="pk-drawer-head">${icon(project.icon)}<h2 id="drawer-title">${esc(project.name)}</h2><button class="pk-button pk-close" type="button" data-close data-key="close" aria-label="Close details">×</button></div><div class="pk-drawer-body"><p class="drawer-lead">${esc(project.description)}</p><section><h3 class="pk-section-label">Status</h3>${rows([
    ["Applications", `${up} of ${total} reachable`],
    ["Configured at", doc ? utc(doc.configuredAt) : "Status unavailable"],
    ...featureRows(doc),
  ])}</section>${list ? `<section><h3 class="pk-section-label">Supporting components</h3><ul class="pk-list">${list}</ul></section>` : ""}<section><h3 class="pk-section-label">Links</h3><div class="drawer-links">${external(repoUrl(project.repo), `${esc(project.name)} on GitHub`, "project-github")}</div></section></div>`;
}

// Replace markup only when it changed, and keep focus on the element with the same data-key.
const rendered = new WeakMap();
function patch(element, html) {
  if (rendered.get(element) === html) return;
  rendered.set(element, html);
  if (!element.contains(document.activeElement)) return void (element.innerHTML = html);
  const key = document.activeElement.dataset.key || "";
  element.innerHTML = html;
  // A control that disappeared hands focus to the drawer's close button, if this is the drawer.
  (element.querySelector(`[data-key="${CSS.escape(key)}"]`) || element.querySelector("[data-close]"))?.focus();
}
// Live regions announce every write, so text changes only when it differs.
function text(selector, value) {
  if ($(selector).textContent !== value) $(selector).textContent = value;
}
function render() {
  patch($("#projects"), DATA.projects.map(projectSection).join(""));
  const all = DATA.projects.map(reachable);
  text("#summary", !checkedText && checking ? "Checking…" : `${all.reduce((n, [up]) => n + up, 0)} of ${all.reduce((n, [, total]) => n + total, 0)} reachable`);
  $("#refresh").disabled = checking;
  text("#refresh", checking ? "Checking…" : "Refresh");
  text("#checked", checkedText);
  const notice = accessNotice();
  $("#notice").hidden = !notice;
  text("#notice", notice);
  if (drawer) {
    const { project, app } = find(drawer.project, drawer.app);
    patch($("#drawer"), app ? appDrawer(project, app) : projectDrawer(project));
  }
}
function openDrawer(target, trigger) {
  const [projectId, appId] = target.split("/");
  drawer = { project: projectId, app: appId, trigger };
  render();
  $("#drawer").showModal();
  $("#drawer [data-close]").focus();
}
async function probe(name) {
  try {
    const response = await fetch(`/health/${name}`, {
      cache: "no-store",
      credentials: "omit",
      redirect: "error",
      signal: AbortSignal.timeout(4000),
    });
    const code = response.status;
    health[name] = {
      state: code === 200 ? "healthy" : [502, 503, 504].includes(code) ? "unreachable" : "unknown",
      detail: `HTTP ${code}`,
    };
  } catch (error) {
    health[name] = { state: "unreachable", detail: error.name === "TimeoutError" ? "timed out" : "no response" };
  }
}
async function check() {
  if (checking) return;
  checking = true;
  render();
  let settingsOK = true;
  const jobs = Object.keys(StackStatus.ids).map((stack) => async () => {
    try {
      documents[stack] = StackStatus.parse((await StackStatus.request(`/stack-status/${stack}`)).text, stack);
    } catch {
      // An absent or invalid document is unknown, never a previous answer.
      delete documents[stack];
    }
    render();
  });
  jobs.push(async () => {
    try {
      config = validateConfig(JSON.parse((await StackStatus.request("/edge-config.json")).text));
    } catch {
      settingsOK = false;
    }
    render();
  });
  try {
    await StackStatus.pool(jobs);
    const names = new Set();
    for (const project of DATA.projects) {
      for (const app of project.apps) {
        const enabled = component(project, app.status).state === "configured";
        for (const name of app.health) {
          if (enabled) names.add(name);
          else delete health[name];
        }
      }
    }
    render();
    await StackStatus.pool([...names].map((name) => async () => {
      await probe(name);
      render();
    }));
    $("#host-name").textContent = location.hostname;
    $("#access-mode").textContent =
      tailnetHost() && location.hostname === tailnetHost()
        ? "Tailscale"
        : location.protocol === "https:"
          ? "HTTPS"
          : "Local HTTP";
    checkedText = `${settingsOK ? "Checked" : "Addresses could not refresh · checked"} ${new Date().toLocaleTimeString()}`;
  } finally {
    checking = false;
    render();
  }
}

document.addEventListener("click", async (e) => {
  const copy = e.target.closest("[data-copy]");
  if (copy) {
    try {
      await navigator.clipboard.writeText(copy.dataset.copy);
      $("#announce").textContent = `Copied ${copy.dataset.copy}`;
      copy.dataset.copied = "";
      setTimeout(() => delete copy.dataset.copied, 1500);
    } catch {
      $("#announce").textContent = "Copy failed. Select the endpoint text instead.";
    }
    return;
  }
  const open = e.target.closest("[data-open]");
  if (open) return openDrawer(open.dataset.open, open.dataset.key);
  if (e.target.closest("#refresh")) return check();
  // A click on the dialog element itself is a click on its backdrop.
  if (e.target.closest("[data-close]") || e.target === $("#drawer")) $("#drawer").close();
});
// A modal dialog still lets Tab leave for the browser chrome; keep focus inside instead.
$("#drawer").addEventListener("keydown", (e) => {
  const items = [...$("#drawer").querySelectorAll("a[href], button")];
  if (e.key !== "Tab" || document.activeElement !== (e.shiftKey ? items[0] : items.at(-1))) return;
  e.preventDefault();
  (e.shiftKey ? items.at(-1) : items[0]).focus();
});
$("#drawer").addEventListener("close", () => {
  const trigger = drawer?.trigger;
  drawer = null;
  $(`[data-key="${CSS.escape(trigger || "")}"]`)?.focus();
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
render();
check();
