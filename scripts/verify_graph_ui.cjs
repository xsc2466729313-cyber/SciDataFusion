const assert = require("node:assert/strict");
const path = require("node:path");

const playwrightPath = process.env.PLAYWRIGHT_CORE_PATH || "playwright-core";
const { chromium } = require(playwrightPath);

const baseUrl = process.argv[2] || "http://127.0.0.1:8000";
const desktopScreenshot = path.resolve(
  process.argv[3] || "docs/assets/workbench-knowledge-graph.jpg",
);
const mobileScreenshot = path.resolve(
  process.argv[4] || "var/workbench-knowledge-graph-mobile.jpg",
);
const overviewScreenshot = path.resolve(
  process.argv[5] || "docs/assets/workbench-overview.jpg",
);

async function inspectGraph(page, screenshotPath, verifyDrag) {
  const browserErrors = [];
  page.on("console", message => {
    if (message.type() === "error") browserErrors.push(message.text());
  });
  page.on("pageerror", error => browserErrors.push(error.message));
  await page.goto(baseUrl, { waitUntil: "networkidle" });
  await page.waitForFunction(() => document.getElementById("status").textContent !== "正在加载");
  assert.equal(await page.locator("#run-form [required]").count(), 1);
  assert.equal(await page.locator("#query").getAttribute("required"), null);
  assert.ok((await page.locator("#blueprint-priorities li").count()) >= 3);
  if (verifyDrag) {
    await page.locator("#goal").fill("我想研究城市热岛效应与绿地覆盖率之间的关系");
    await page.locator("#run").click();
    await page.waitForFunction(() => !document.getElementById("run").disabled);
    await page.waitForFunction(() => document.getElementById("blueprint-title").textContent.includes("城市热岛"));
    await page.screenshot({
      path: overviewScreenshot,
      type: "jpeg",
      quality: 90,
    });
  }
  await page.locator('button[data-view="quality"]').click();
  const canvas = page.locator("#evidence-graph");
  await canvas.scrollIntoViewIfNeeded();
  try {
    await page.waitForFunction(() => {
      const element = document.getElementById("evidence-graph");
      return element?.dataset.graphReady === "true"
        && Number(element.dataset.renderedFrames || 0) >= 20;
    });
  } catch (error) {
    const graphState = await canvas.evaluate(element => ({ ...element.dataset }));
    throw new Error(
      `graph initialization failed: ${JSON.stringify({ graphState, browserErrors })}`,
      { cause: error },
    );
  }
  await page.locator("#graph-inspector h3").waitFor();

  const layout = await page.evaluate(() => ({
    viewportWidth: window.innerWidth,
    pageWidth: document.documentElement.scrollWidth,
    selectedNode: document.getElementById("evidence-graph").dataset.selectedNode,
    activeNodes: Number(document.getElementById("evidence-graph").dataset.activeNodes),
    activeEdges: Number(document.getElementById("evidence-graph").dataset.activeEdges),
  }));
  assert.ok(layout.pageWidth <= layout.viewportWidth, `page overflow: ${JSON.stringify(layout)}`);
  assert.ok(layout.selectedNode);
  assert.equal(layout.activeNodes, 87);
  assert.equal(layout.activeEdges, 130);

  const pixels = await page.evaluate(() => {
    const element = document.getElementById("evidence-graph");
    const context = element.getContext("webgl2") || element.getContext("webgl");
    if (!context) return { colorBuckets: 0, sampledPixels: 0 };
    const buffer = new Uint8Array(element.width * element.height * 4);
    context.readPixels(0, 0, element.width, element.height, context.RGBA, context.UNSIGNED_BYTE, buffer);
    const buckets = new Set();
    let sampledPixels = 0;
    const stride = Math.max(1, Math.floor(element.width / 160));
    for (let y = 0; y < element.height; y += stride) {
      for (let x = 0; x < element.width; x += stride) {
        const offset = (y * element.width + x) * 4;
        buckets.add(`${buffer[offset] >> 4}:${buffer[offset + 1] >> 4}:${buffer[offset + 2] >> 4}`);
        sampledPixels += 1;
      }
    }
    return { colorBuckets: buckets.size, sampledPixels };
  });
  assert.ok(pixels.sampledPixels > 1_000, `canvas sample too small: ${JSON.stringify(pixels)}`);
  assert.ok(pixels.colorBuckets >= 8, `canvas appears blank: ${JSON.stringify(pixels)}`);

  await page.locator("#graph-pause").click();
  await page.waitForFunction(() => document.getElementById("graph-status").textContent.includes("布局已暂停"));
  await page.waitForTimeout(250);
  if (verifyDrag) {
    const box = await canvas.boundingBox();
    assert.ok(box);
    let fixed = false;
    for (const [dx, dy] of [[28, 18], [45, -20], [-32, 24]]) {
      const selected = await canvas.evaluate(element => ({
        x: Number(element.dataset.selectedX),
        y: Number(element.dataset.selectedY),
      }));
      assert.ok(Number.isFinite(selected.x) && Number.isFinite(selected.y));
      assert.ok(selected.x >= 0 && selected.x <= box.width && selected.y >= 0 && selected.y <= box.height);
      await page.mouse.move(box.x + selected.x, box.y + selected.y);
      await page.mouse.down();
      await page.mouse.move(box.x + selected.x + dx, box.y + selected.y + dy, { steps: 8 });
      await page.mouse.up();
      try {
        await page.waitForFunction(
          () => document.getElementById("graph-status").textContent.includes("已固定"),
          null,
          { timeout: 1500 },
        );
        fixed = true;
        break;
      } catch {}
    }
    assert.ok(fixed, "selected graph node could not be dragged and pinned");
  }

  const beforeFilter = Number(await canvas.getAttribute("data-active-nodes"));
  await page.locator('.graph-filter[data-kind="evidence"]').click();
  const afterFilter = Number(await canvas.getAttribute("data-active-nodes"));
  assert.ok(afterFilter < beforeFilter, `${beforeFilter} nodes remained after filtering`);
  await page.locator('.graph-filter[data-kind="evidence"]').click();
  await page.locator("#graph-pause").click();
  await page.waitForFunction(() => document.getElementById("graph-status").textContent.includes("3D 布局运行中"));
  await page.waitForTimeout(250);
  await page.locator("#knowledge-graph").screenshot({
    path: screenshotPath,
    type: "jpeg",
    quality: 90,
  });
  assert.deepEqual(browserErrors, []);
  return { layout, pixels, afterFilter };
}

(async () => {
  const browser = await chromium.launch({
    headless: true,
    executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE || undefined,
    args: ["--enable-webgl", "--use-angle=swiftshader"],
  });
  try {
    const desktop = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    const desktopResult = await inspectGraph(desktop, desktopScreenshot, true);
    await desktop.close();
    const mobile = await browser.newPage({ viewport: { width: 390, height: 844 } });
    const mobileResult = await inspectGraph(mobile, mobileScreenshot, false);
    await mobile.close();
    process.stdout.write(`${JSON.stringify({ desktopResult, mobileResult })}\n`);
  } finally {
    await browser.close();
  }
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
