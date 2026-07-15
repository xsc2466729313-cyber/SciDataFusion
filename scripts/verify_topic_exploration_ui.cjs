const assert = require("node:assert/strict");
const path = require("node:path");

const playwrightPath = process.env.PLAYWRIGHT_CORE_PATH || "playwright-core";
const { chromium } = require(playwrightPath);

const baseUrl = process.argv[2] || "http://127.0.0.1:8000";
const overviewScreenshot = path.resolve(
  process.argv[3] || "docs/assets/workbench-online-exploration-v1.2.jpg",
);
const graphScreenshot = path.resolve(
  process.argv[4] || "docs/assets/workbench-online-graph-v1.2.jpg",
);

async function inspect(page, takeScreenshots) {
  const browserErrors = [];
  page.on("console", message => {
    if (message.type() === "error") browserErrors.push(message.text());
  });
  page.on("pageerror", error => browserErrors.push(error.message));
  await page.goto(baseUrl, { waitUntil: "networkidle" });
  await page.waitForFunction(() => document.getElementById("topic-data-status").textContent === "真实来源探索态");
  assert.equal(await page.locator("#mode-online").isChecked(), true);
  assert.match(await page.locator("#blueprint-title").textContent(), /城市|热岛/);
  assert.ok((await page.locator("#blueprint-fields li").count()) >= 3);
  assert.ok((await page.locator("#online-source-rows tr").count()) >= 1);
  assert.match(await page.locator("#delivery-rows").textContent(), /没有可冒充正式成果的数据文件/);
  assert.equal(await page.locator("#download-package").isDisabled(), true);
  if (takeScreenshots) {
    await page.screenshot({ path: overviewScreenshot, type: "jpeg", quality: 90 });
  }

  await page.locator('button[data-view="quality"]').click();
  const canvas = page.locator("#evidence-graph");
  await canvas.scrollIntoViewIfNeeded();
  await page.waitForFunction(() => {
    const element = document.getElementById("evidence-graph");
    return element?.dataset.graphReady === "true" && Number(element.dataset.renderedFrames || 0) >= 20;
  });
  const graph = await page.evaluate(() => ({
    activeNodes: Number(document.getElementById("evidence-graph").dataset.activeNodes),
    activeEdges: Number(document.getElementById("evidence-graph").dataset.activeEdges),
    viewportWidth: window.innerWidth,
    pageWidth: document.documentElement.scrollWidth,
  }));
  assert.ok(graph.activeNodes >= 10, JSON.stringify(graph));
  assert.ok(graph.activeEdges >= 9, JSON.stringify(graph));
  assert.ok(graph.pageWidth <= graph.viewportWidth, JSON.stringify(graph));
  if (takeScreenshots) {
    await page.locator("#knowledge-graph").screenshot({
      path: graphScreenshot,
      type: "jpeg",
      quality: 90,
    });
  }
  assert.deepEqual(browserErrors, []);
  return graph;
}

(async () => {
  const browser = await chromium.launch({
    headless: true,
    executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE || undefined,
    args: ["--enable-webgl", "--use-angle=swiftshader"],
  });
  try {
    const desktop = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    const desktopResult = await inspect(desktop, true);
    await desktop.close();
    const mobile = await browser.newPage({ viewport: { width: 390, height: 844 } });
    const mobileResult = await inspect(mobile, false);
    await mobile.close();
    process.stdout.write(`${JSON.stringify({ desktopResult, mobileResult })}\n`);
  } finally {
    await browser.close();
  }
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
