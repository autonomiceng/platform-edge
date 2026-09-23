// Run with an installed Playwright package; PLAYWRIGHT_MODULE may name its absolute path.
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const fs = require("node:fs");
const assert = require("node:assert/strict");
(async () => {
  const browser = await chromium.launch();
  try {
    const page = await browser.newPage();
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    const now = new Date("2026-09-23T17:00:00Z");
    await page.clock.install({ time: now });
    const fixture = (stack) =>
      JSON.parse(
        fs.readFileSync(`docs/operations/status-fixtures/v2-${stack}.json`, "utf8"),
      );
    // Backplane serves this document; the other stacks serve their fixtures.
    let backplaneStatus = fixture("backplane"),
      statusAvailable = true,
      requests = 0;
    await page
      .context()
      .grantPermissions(["clipboard-read", "clipboard-write"]);
    const config = {
      domain: "localhost",
      mode: "local",
      scheme: "http",
      tailscale: "test.ts.net",
      connected: "gateway",
      ports: {
        gateway: "8446",
        litellm: "8443",
        langfuse: "8444",
        s3: "8445",
        observability: "8447",
        backplane: "8448",
        rustfs: "8449",
      },
    };
    let backplaneReady = true;
    await page.route("https://test.ts.net/**", (route) => {
      const path = new URL(route.request().url()).pathname;
      requests++;
      if (path.startsWith("/stack-status/")) {
        const stack = path.slice("/stack-status/".length);
        if (!statusAvailable || stack === "gateway")
          return route.fulfill({
            status: 404,
            contentType: "application/json",
            body: "",
          });
        return route.fulfill({
          contentType: "application/json",
          body: JSON.stringify(
            stack === "backplane" ? backplaneStatus : fixture(stack),
          ),
        });
      }
      if (path === "/edge-config.json") return route.fulfill({ json: config });
      if (path.startsWith("/health"))
        return route.fulfill({
          status: path === "/health/backplane" && !backplaneReady ? 502 : 200,
          body: "",
        });
      if (path.startsWith("/console/")) {
        const file = "docker/console/" + path.slice("/console/".length);
        const ext = file.split(".").pop();
        return route.fulfill({
          body: fs.readFileSync(file),
          contentType: {
            js: "application/javascript",
            css: "text/css",
            svg: "image/svg+xml",
            png: "image/png",
          }[ext],
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
    const settled = () =>
      page.waitForFunction(() => !document.querySelector("#refresh").disabled);
    const refresh = async () => {
      await page.getByRole("button", { name: "Refresh", exact: true }).click();
      await settled();
    };
    await page.goto("https://test.ts.net/");
    await settled();
    assert.equal(
      await page.locator(".version").first().textContent(),
      "Configured 2.11.4",
      "The Edge card shows the version its bootstrap configured",
    );
    assert.equal(
      await page.locator(".project").first().locator(".features").textContent(),
      "Backups configured · no checkpoint recorded",
    );
    assert.equal(
      await page.locator(".project h2").first().textContent(),
      "Platform Edge",
    );
    assert.equal(
      await page
        .getByRole("link", { name: "Backplane ↗", exact: true })
        .count(),
      0,
    );
    config.connected += ",backplane";
    await refresh();
    assert.equal(
      await page
        .getByRole("link", { name: "Backplane ↗", exact: true })
        .getAttribute("href"),
      "https://test.ts.net:8448/dashboard/",
    );
    await page
      .getByRole("button", { name: "Copy Backplane API", exact: true })
      .click();
    assert.equal(
      await page.evaluate(() => navigator.clipboard.readText()),
      "https://test.ts.net:8448/api/v1",
    );
    assert.ok(
      !(await page.locator(".version").allTextContents()).some((text) =>
        text.startsWith("Configured 1.101.0"),
      ),
      "A missing Gateway producer never borrows another version source",
    );
    config.connected = "gateway";
    await page.clock.fastForward(30000);
    await settled();
    assert.equal(
      await page
        .getByRole("link", { name: "Backplane ↗", exact: true })
        .count(),
      0,
    );
    config.connected += ",backplane";
    await page.clock.fastForward(30000);
    await settled();
    assert.equal(
      await page
        .getByRole("link", { name: "Backplane ↗", exact: true })
        .count(),
      1,
    );
    config.domain = null;
    await refresh();
    assert.equal(
      await page
        .getByRole("link", { name: "Backplane ↗", exact: true })
        .count(),
      1,
    );
    assert.match(
      await page.locator("#checked").textContent(),
      /could not refresh/,
    );
    config.domain = "localhost";
    backplaneReady = false;
    await refresh();
    assert.equal(
      await page
        .getByRole("link", { name: "Backplane ↗", exact: true })
        .count(),
      1,
      "Trusted navigation survives failed reachability",
    );
    backplaneReady = true;
    await refresh();
    const bpCard = page
      .locator(".app-card")
      .filter({
        has: page.getByRole("link", { name: "Backplane ↗", exact: true }),
      });
    await refresh();
    assert.equal(await bpCard.locator(".badge").textContent(), "Configured");
    assert.equal(await bpCard.locator(".version").textContent(), "Configured 0.9.0");
    assert.match(
      await bpCard.locator(".evidence").textContent(),
      /^Configuration 2026-09-23T16:10:00.000Z · HTTP reachability: reachable$/,
    );
    assert.ok(
      (await page.locator(".inventory .badge.off").count()) >= 3,
      "Disabled optional components render Not enabled",
    );
    for (const mutate of [
      (d) => (d.contract = 1),
      (d) => (d.components[0].observedVersion = "0.9.0"),
      (d) => d.components.push({ ...d.components[1] }),
    ]) {
      backplaneStatus = fixture("backplane");
      mutate(backplaneStatus);
      await refresh();
      assert.equal(await bpCard.locator(".badge").textContent(), "Unknown");
      assert.equal(await bpCard.locator(".version").textContent(), "Version unavailable");
      assert.equal(await bpCard.locator("h3 a").count(), 1);
    }
    backplaneStatus = fixture("backplane");
    await refresh();
    statusAvailable = false;
    await refresh();
    assert.equal(await bpCard.locator(".badge").textContent(), "Unknown");
    assert.match(
      await bpCard.locator(".evidence").textContent(),
      /^Status unavailable · HTTP reachability: reachable$/,
    );
    statusAvailable = true;
    await refresh();
    // Hidden pages stop network polling.
    await page.evaluate(() =>
      Object.defineProperty(document, "hidden", {
        configurable: true,
        value: true,
      }),
    );
    const hiddenRequests = requests;
    await page.clock.fastForward(61000);
    assert.equal(requests, hiddenRequests);
    assert.equal(await bpCard.locator(".badge").textContent(), "Configured");
    await page.evaluate(() => {
      Object.defineProperty(document, "hidden", {
        configurable: true,
        value: false,
      });
      document.dispatchEvent(new Event("visibilitychange"));
    });
    await settled();
    assert.ok(requests > hiddenRequests);
    statusAvailable = false;
    await refresh();
    await page
      .getByRole("textbox", { name: "Find a service" })
      .fill("postgres");
    assert.equal(await page.locator(".project").count(), 2);
    await page.clock.fastForward(30000);
    await settled();
    assert.equal(
      await page.getByRole("textbox", { name: "Find a service" }).inputValue(),
      "postgres",
    );
    assert.equal(
      await page.locator(".inventory .badge.healthy").count(),
      0,
      "Unprobed databases must not inherit app health",
    );
    const search = page.getByRole("textbox", { name: "Find a service" });
    await search.press("ControlOrMeta+A");
    await search.press("Backspace");
    assert.equal(await search.inputValue(), "");
    await page
      .getByRole("button", { name: "Details →", exact: true })
      .first()
      .click();
    assert.equal(
      await page.locator(".detail .github-link").getAttribute("href"),
      "https://github.com/caddyserver/caddy",
    );
    await page.keyboard.press("Escape");
    assert.equal(await page.getByRole("dialog").count(), 0);
    await page.getByRole("button", { name: "Map", exact: true }).click();
    assert.equal(
      await page.locator('.node[aria-label*="Prepare object storage"]').count(),
      0,
    );
    await page.getByLabel("Application connections", { exact: true }).uncheck();
    assert.equal(await page.locator(".edge.app").count(), 0);
    assert.ok((await page.locator(".edge.logs").count()) > 0);
    await page
      .getByLabel("Log collection (when configured)", { exact: true })
      .uncheck();
    assert.equal(await page.locator(".edge").count(), 0);
    await page
      .getByRole("button", { name: "Clear selection", exact: true })
      .click();
    assert.equal(await page.locator(".node.selected").count(), 0);
    await page.setViewportSize({ width: 390, height: 844 });
    assert.equal(
      await page.evaluate(
        () => document.documentElement.scrollWidth > innerWidth,
      ),
      false,
    );
    assert.deepEqual(errors, []);
    console.log(
      "PASS: contract 2 documents, invalid and missing producers, configured versions, Not enabled, features, hidden polling, trusted links, live refresh, search, details, map, and mobile layout",
    );
  } finally {
    await browser.close();
  }
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
