// Run with an installed Playwright package; PLAYWRIGHT_MODULE may name its absolute path.
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const fs = require("node:fs");
const assert = require("node:assert/strict");
(async () => {
  const browser = await chromium.launch();
  try {
    const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.clock.install({ time: new Date("2026-09-23T17:00:00Z") });
    await page.context().grantPermissions(["clipboard-read", "clipboard-write"]);
    const fixture = (stack) =>
      JSON.parse(fs.readFileSync(`docs/operations/status-fixtures/v2-${stack}.json`, "utf8"));
    const config = {
      domain: "localhost",
      mode: "local",
      scheme: "http",
      tailnet: "",
      root: "",
      apps: "console,litellm,langfuse,s3,rustfs,backplane,grafana",
    };
    // Each Health Path answers with its own status code.
    const documents = {
      edge: fixture("edge"),
      gateway: fixture("gateway"),
      backplane: fixture("backplane"),
      observability: fixture("observability"),
    };
    const probes = { caddy: 200, litellm: 404, langfuse: 200, s3: 200, rustfs: 503, backplane: 502, observability: 200 };
    let requests = 0;
    const healthRequests = new Map();
    const healthCount = (name) => healthRequests.get(name) || 0;
    // The Tailnet console address and the public-domain one serve the same files.
    await page.route(/^(https:\/\/platform\.test\.ts\.net|http:\/\/localhost)\//, (route) => {
      const path = new URL(route.request().url()).pathname;
      requests++;
      if (path.startsWith("/stack-status/")) {
        const doc = documents[path.slice("/stack-status/".length)];
        return doc
          ? route.fulfill({ contentType: "application/json", body: JSON.stringify(doc) })
          : route.fulfill({ status: 404, contentType: "application/json", body: "" });
      }
      if (path === "/edge-config.json") return route.fulfill({ json: config });
      if (path.startsWith("/health/")) {
        const name = path.slice("/health/".length);
        healthRequests.set(name, healthCount(name) + 1);
        return route.fulfill({ status: probes[name] ?? 404, body: "" });
      }
      if (path.startsWith("/console/")) {
        const file = "docker/console/" + path.slice("/console/".length);
        return route.fulfill({
          body: fs.readFileSync(file),
          contentType: { js: "application/javascript", css: "text/css", svg: "image/svg+xml", png: "image/png" }[
            file.split(".").pop()
          ],
        });
      }
      const html = fs
        .readFileSync("docker/console/index.html", "utf8")
        .replace(
          /(<script type="application\/json" id="edge-config">)[\s\S]*?(<\/script>)/,
          "$1" + JSON.stringify(config) + "$2",
        );
      return route.fulfill({ contentType: "text/html", body: html });
    });
    const settled = () => page.waitForFunction(() => !document.querySelector("#refresh").disabled);
    const refresh = async () => {
      await page.getByRole("button", { name: "Refresh", exact: true }).click();
      await settled();
    };
    const card = (name) => page.locator(".pk-app").filter({ has: page.locator("h3", { hasText: name }) });
    const badge = async (name) => (await card(name).locator(".pk-badge").textContent()).trim();
    const backplaneLink = () => page.getByRole("link", { name: "Backplane ↗", exact: true });

    await page.goto("https://platform.test.ts.net/");
    await settled();

    // Cards, badges per state and configured versions.
    assert.equal(await page.locator(".pk-app").count(), 6);
    assert.deepEqual(await page.locator(".project h2").allTextContents(), [
      "Platform Edge",
      "Agent Backplane",
      "Observability",
      "LLM Gateway",
    ]);
    assert.equal(await badge("Caddy"), "Healthy");
    assert.equal(await badge("Backplane"), "Unreachable");
    assert.equal(await badge("Grafana"), "Healthy");
    assert.equal(await badge("RustFS"), "Degraded");
    assert.equal(await badge("LiteLLM"), "Unknown", "404 is no evidence");
    assert.equal(await card("Caddy").locator(".version").textContent(), "Configured 2.11.4");
    assert.equal(await card("Backplane").locator(".version").textContent(), "Configured 0.9.0");
    assert.equal(await card("Langfuse").locator(".version").textContent(), "Configured 4.37.0");
    assert.equal(await page.locator("#summary").textContent(), "4 of 6 reachable");
    assert.equal(await page.locator('[data-project="observability"] .reachable').textContent(), "1 of 1 app reachable");

    // Links: none on an address the configuration does not name; Tailnet Origins once recorded.
    assert.equal(await backplaneLink().count(), 0);
    assert.ok(await page.locator("#notice").isVisible());
    config.tailnet = "test.ts.net";
    await refresh();
    assert.ok(await page.locator("#notice").isHidden());
    assert.equal(await backplaneLink().getAttribute("href"), "https://backplane.test.ts.net/dashboard/");
    await page.getByRole("button", { name: "Copy Backplane API", exact: true }).click();
    assert.equal(await page.evaluate(() => navigator.clipboard.readText()), "https://backplane.test.ts.net/api/v1");
    assert.equal(await page.locator("#announce").textContent(), "Copied https://backplane.test.ts.net/api/v1");
    // S3 is a copyable endpoint, never a browser link; the missing Gateway producer keeps links.
    assert.equal(await card("RustFS").locator('a[href^="https://s3."]').count(), 0);
    assert.equal(
      await card("RustFS").getByRole("button", { name: "Copy RustFS S3", exact: true }).getAttribute("data-copy"),
      "https://s3.test.ts.net",
    );
    assert.equal(await card("LiteLLM").getByRole("link").getAttribute("href"), "https://litellm.test.ts.net/ui/");
    // The public-domain config on the Tailnet console leaves only Tailnet Origins it can trust.
    config.apps = "console,litellm";
    await refresh();
    assert.equal(await backplaneLink().count(), 0);
    assert.equal(await card("Backplane").locator(".pk-endpoint").count(), 0);
    config.apps = "console,litellm,langfuse,s3,rustfs,backplane,grafana";
    await refresh();
    config.domain = null;
    await refresh();
    assert.equal(await backplaneLink().count(), 1, "invalid settings keep the last trusted links");
    assert.match(await page.locator("#checked").textContent(), /could not refresh/);
    config.domain = "localhost";

    // An invalid component stays Unknown and skips its probe while valid neighbors still probe.
    const litellmBeforeInvalid = healthCount("litellm");
    const langfuseBeforeInvalid = healthCount("langfuse");
    documents.gateway.components.find((c) => c.id === "litellm").url = "https://litellm.example.com/ui/";
    await refresh();
    assert.equal(await badge("LiteLLM"), "Unknown");
    assert.equal(healthCount("litellm"), litellmBeforeInvalid);
    assert.equal(healthCount("langfuse"), langfuseBeforeInvalid + 1);
    documents.gateway = fixture("gateway");

    // Disabled comes from the document; a missing or invalid document is Unknown with links intact.
    const litellmBeforeMissing = healthCount("litellm");
    delete documents.gateway;
    await refresh();
    assert.equal(healthCount("litellm"), litellmBeforeMissing);
    for (const name of ["LiteLLM", "Langfuse", "RustFS"]) assert.equal(await badge(name), "Unknown", name);
    assert.equal(await card("Langfuse").locator(".version").textContent(), "Version unknown");
    assert.equal(await card("Langfuse").getByRole("link").getAttribute("href"), "https://langfuse.test.ts.net/");
    documents.gateway = fixture("gateway");
    documents.backplane.components[0].enabled = false;
    const backplaneBeforeDisabled = healthCount("backplane");
    await refresh();
    assert.equal(await badge("Backplane"), "Disabled");
    assert.equal(healthCount("backplane"), backplaneBeforeDisabled);
    documents.backplane.contract = 1;
    await refresh();
    assert.equal(await badge("Backplane"), "Unknown");
    assert.equal(healthCount("backplane"), backplaneBeforeDisabled);
    assert.equal(await card("Backplane").locator(".version").textContent(), "Version unknown");
    assert.equal(await backplaneLink().count(), 1);
    documents.backplane = fixture("backplane");
    probes.backplane = 200;
    await refresh();
    assert.equal(await badge("Backplane"), "Healthy");

    // App drawer: open, focus, trap, Escape, focus returns to its trigger.
    const trigger = card("Backplane").getByRole("button", { name: "Details", exact: true });
    await trigger.focus();
    await page.keyboard.press("Enter");
    const dialog = page.getByRole("dialog", { name: "Backplane" });
    assert.ok(await dialog.isVisible());
    assert.equal(await page.evaluate(() => document.activeElement.getAttribute("aria-label")), "Close details");
    assert.match(await dialog.textContent(), /Imageghcr\.io\/autonomiceng\/agent-backplane-server:0\.9\.0Version0\.9\.0Configured at2026-09-23 16:10 UTC/);
    assert.match(await dialog.textContent(), /\/health\/backplane · HTTP 200/);
    for (let i = 0; i < 8; i++) {
      await page.keyboard.press("Tab");
      assert.ok(await page.evaluate(() => document.querySelector("#drawer").contains(document.activeElement)), `Tab ${i}`);
    }
    await page.keyboard.press("Shift+Tab");
    assert.ok(await page.evaluate(() => document.querySelector("#drawer").contains(document.activeElement)));
    // Changed content while open keeps the focus on the same control.
    await dialog.getByRole("button", { name: "Close details" }).focus();
    probes.backplane = 503;
    await page.clock.fastForward(30000);
    await settled();
    assert.equal((await dialog.locator(".pk-badge").textContent()).trim(), "Unreachable");
    assert.equal(await page.evaluate(() => document.activeElement.getAttribute("aria-label")), "Close details");
    // A refresh while open keeps the drawer and the focused control.
    await dialog.getByRole("button", { name: "Copy Backplane API", exact: true }).focus();
    await page.clock.fastForward(30000);
    await settled();
    assert.equal(await page.evaluate(() => document.activeElement.getAttribute("aria-label")), "Copy Backplane API");
    await page.keyboard.press("Escape");
    assert.equal(await page.getByRole("dialog").count(), 0);
    // The close event, which returns focus to the trigger, is dispatched as a task.
    await page.waitForFunction(() => document.activeElement.dataset.key === "details-backplane-backplane");

    // Project drawer: supporting components live here, not on the page.
    await page.getByRole("button", { name: "LLM Gateway details", exact: true }).click();
    const project = page.getByRole("dialog", { name: "LLM Gateway" });
    assert.equal(await project.locator(".pk-list li").count(), 7);
    assert.equal(await page.locator("#projects .pk-list").count(), 0);
    assert.match(await project.textContent(), /BackupsConfigured · last checkpoint 2026-09-22 03:00 UTC/);
    await project.getByRole("button", { name: "Close details" }).click();
    assert.equal(await page.getByRole("dialog").count(), 0);
    await page.getByRole("button", { name: "Observability details", exact: true }).click();
    assert.equal(await page.getByRole("dialog").locator('.pk-badge[data-state="disabled"]').count(), 1);
    await page.mouse.click(40, 450);
    assert.equal(await page.getByRole("dialog").count(), 0, "a backdrop click closes the drawer");

    // Visible focus ring, both schemes, reduced motion.
    await page.keyboard.press("Tab");
    assert.equal(await page.evaluate(() => getComputedStyle(document.activeElement).outlineStyle), "solid");
    const surface = () => page.evaluate(() => getComputedStyle(document.body).backgroundColor);
    const transition = () =>
      page.evaluate(() => getComputedStyle(document.querySelector("#refresh")).transitionDuration);
    await page.emulateMedia({ colorScheme: "dark", reducedMotion: "no-preference" });
    assert.equal(await surface(), "rgb(13, 21, 37)");
    assert.notEqual(await transition(), "0s");
    await page.emulateMedia({ colorScheme: "light", reducedMotion: "reduce" });
    assert.equal(await surface(), "rgb(243, 246, 250)");
    assert.equal(await transition(), "0s");
    assert.equal(await badge("Caddy"), "Healthy");

    // Hidden pages stop network polling and resume on visibility.
    await page.evaluate(() => Object.defineProperty(document, "hidden", { configurable: true, value: true }));
    const hiddenRequests = requests;
    await page.clock.fastForward(61000);
    assert.equal(requests, hiddenRequests);
    await page.evaluate(() => {
      Object.defineProperty(document, "hidden", { configurable: true, value: false });
      document.dispatchEvent(new Event("visibilitychange"));
    });
    await settled();
    assert.ok(requests > hiddenRequests);

    // The public-domain console links to public-domain origins when no tailnet is recorded.
    config.tailnet = "";
    await page.goto("http://localhost/");
    await settled();
    assert.equal(await backplaneLink().getAttribute("href"), "http://backplane.localhost/dashboard/");
    assert.equal(await page.locator("#access-mode").textContent(), "Local HTTP");
    for (const width of [1000, 390]) {
      await page.setViewportSize({ width, height: 844 });
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, `${width}px`);
    }
    assert.deepEqual(errors, []);
    console.log(
      "PASS: cards and badge states, versions, Tailnet and public links, S3 endpoint, invalid and missing producers, drawers with focus trap and Escape, focus ring, light scheme, reduced motion, hidden polling, responsive layout",
    );
  } finally {
    await browser.close();
  }
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
