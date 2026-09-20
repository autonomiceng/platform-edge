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
    await page.clock.install();
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
    let versionsAvailable = true,
      backplaneReady = true;
    await page.route("https://test.ts.net/**", (route) => {
      const path = new URL(route.request().url()).pathname;
      if (path === "/edge-config.json") return route.fulfill({ json: config });
      if (path === "/stack-versions/gateway")
        return route.fulfill({
          status: versionsAvailable ? 200 : 502,
          json: { images: { litellm: "1.2.3" } },
        });
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
      (await page.locator(".version").allTextContents()).includes("1.2.3"),
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
    versionsAvailable = false;
    await refresh();
    assert.ok(
      !(await page.locator(".version").allTextContents()).includes("1.2.3"),
    );
    backplaneReady = false;
    await refresh();
    assert.equal(
      await page
        .getByRole("link", { name: "Backplane ↗", exact: true })
        .count(),
      0,
    );
    backplaneReady = true;
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
    await page.getByRole("textbox", { name: "Find a service" }).fill("");
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
      "PASS: live refresh, safe links, independent health, search, service details, map toggles, and mobile layout",
    );
  } finally {
    await browser.close();
  }
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
