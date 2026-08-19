import { chromium } from "@playwright/test";
import { createHash } from "node:crypto";
import { mkdirSync, readFileSync, renameSync, writeFileSync } from "node:fs";
import path from "node:path";

const root = process.env.ARCHON_VIDEO_ROOT;
const releaseSha = process.env.ARCHON_RELEASE_SHA;
const hostedRunId = process.env.ARCHON_HOSTED_RUN_ID;
const governedRunId = process.env.ARCHON_GOVERNED_RUN_ID;
if (!root || !/^[a-f0-9]{40}$/u.test(releaseSha ?? "")) {
  throw new Error("The exact video root and release SHA are required.");
}
if (!/^[1-9][0-9]*$/u.test(hostedRunId ?? "") || !/^[1-9][0-9]*$/u.test(governedRunId ?? "")) {
  throw new Error("Exact hosted and governed run IDs are required.");
}
const captureDir = path.join(root, "capture");
mkdirSync(captureDir, { recursive: false });
const timing = JSON.parse(
  readFileSync(path.join(root, "narration", "timing.json"), "utf8"),
);
const holds = Object.fromEntries(
  timing.scenes.map((scene) => [scene.id, Number(scene.holdSeconds) * 1000]),
);
const expectedScenes = [
  "hook",
  "live",
  "findings",
  "stack",
  "governance",
  "evidence",
  "oss",
  "close",
];
if (JSON.stringify(timing.scenes.map((scene) => scene.id)) !== JSON.stringify(expectedScenes)) {
  throw new Error("The narration and production journey scene order differ.");
}

const browser = await chromium.launch({ args: ["--force-device-scale-factor=1"] });
const context = await browser.newContext({
  viewport: { width: 1920, height: 1080 },
  deviceScaleFactor: 1,
  recordVideo: { dir: path.join(captureDir, "raw"), size: { width: 1920, height: 1080 } },
});
const page = await context.newPage();
const video = page.video();
if (!video) throw new Error("Playwright did not create a video recorder.");
const errors = [];
const onOwnedOrigin = () => page.url().startsWith("https://archon-datahub.web.app/");
page.on("pageerror", (error) => {
  if (onOwnedOrigin()) errors.push(`page:${error.name}`);
});
page.on("console", (message) => {
  if (onOwnedOrigin() && message.type() === "error") errors.push("console:error");
});
const captureStarted = Date.now();
const appUrl = `https://archon-datahub.web.app/?release=${releaseSha}`;
await page.goto(appUrl, { waitUntil: "networkidle", timeout: 60_000 });
await page.getByRole("heading", { name: /Stop governance failures/u }).waitFor();
const timelineStarted = Date.now();

async function holdScene(id, action) {
  const started = Date.now();
  await action();
  await page.waitForTimeout(Math.max(0, holds[id] - (Date.now() - started)));
}

async function scrollTo(selector) {
  await page.locator(selector).scrollIntoViewIfNeeded();
  await page.waitForTimeout(500);
}

await holdScene("hook", async () => {
  await page.evaluate(() => window.scrollTo({ top: 0, behavior: "smooth" }));
});

await holdScene("live", async () => {
  await page.getByRole("button", { name: "Run the live read-only audit" }).click();
  await page
    .getByRole("banner")
    .getByRole("status", { name: "Live DataHub" })
    .waitFor({ timeout: 60_000 });
  if (
    await page
      .getByText("Fixture preview", { exact: true })
      .isVisible()
      .catch(() => false)
  ) {
    throw new Error("The production audit remained in fixture mode.");
  }
});

await holdScene("findings", async () => {
  await scrollTo("#findings");
  const contradiction = page.locator('button:has-text("Contradiction")').first();
  await contradiction.click();
  await page.waitForTimeout(1_200);
  const gap = page.locator('button:has-text("Lineage gap")').first();
  await gap.click();
});

await holdScene("stack", async () => {
  await scrollTo("#agent-stack");
});

await holdScene("governance", async () => {
  await page.goto(
    `https://github.com/upgradedev/archon-datahub/actions/runs/${governedRunId}`,
    { waitUntil: "domcontentloaded", timeout: 60_000 },
  );
  await page.getByText(/Live governed G6 proof/iu).first().waitFor({ timeout: 30_000 });
});

await holdScene("evidence", async () => {
  await page.goto(appUrl, { waitUntil: "networkidle", timeout: 60_000 });
  await scrollTo("#judge-evidence");
  await page.getByRole("button", { name: "Prepare & verify pack" }).click();
  await page.getByText(/Named self-consistency checks/iu).first().waitFor({ timeout: 30_000 });
});

await holdScene("oss", async () => {
  await page.goto("https://github.com/acryldata/mcp-server-datahub/pull/183", {
    waitUntil: "domcontentloaded",
    timeout: 60_000,
  });
  await page.getByText(/get_aspect_history/iu).first().waitFor({ timeout: 30_000 });
});

await holdScene("close", async () => {
  await page.goto(appUrl, { waitUntil: "networkidle", timeout: 60_000 });
  await page.evaluate(() => window.scrollTo({ top: 0, behavior: "smooth" }));
});

await context.close();
await browser.close();
const rawPath = await video.path();
const finalPath = path.join(captureDir, "production.webm");
renameSync(rawPath, finalPath);
const bytes = readFileSync(finalPath);
const receipt = {
  schemaVersion: "archon.submission-video-capture/v1",
  releaseSha,
  hostedRunId: Number(hostedRunId),
  governedRunId: Number(governedRunId),
  sceneCount: expectedScenes.length,
  trimLeadSeconds: Math.max(0, (timelineStarted - captureStarted) / 1000),
  timelineSeconds: Number(timing.totalSeconds),
  pageErrors: errors,
  bytes: bytes.length,
  sha256: createHash("sha256").update(bytes).digest("hex"),
};
writeFileSync(
  path.join(captureDir, "capture-receipt.json"),
  `${JSON.stringify(receipt, null, 2)}\n`,
);
if (errors.length !== 0) throw new Error(`Production journey emitted ${errors.length} browser errors.`);
console.log(JSON.stringify(receipt));
