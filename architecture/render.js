import { spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
process.env.PLAYWRIGHT_BROWSERS_PATH = path.join(__dirname, ".ms-playwright");

const FFMPEG = "/opt/anaconda3/envs/lineguard-video/bin/ffmpeg";
const FFPROBE = "/opt/anaconda3/envs/lineguard-video/bin/ffprobe";
const WIDTH = 1920;
const HEIGHT = 1080;
const FPS = 30;
const DURATION = 21;
const FRAME_COUNT = FPS * DURATION;

const outputDir = path.join(__dirname, "output");
const frameDir = path.join(outputDir, "frames");
const fullMp4 = path.join(outputDir, "02_Architecture.mp4");
const previewMp4 = path.join(outputDir, "02_Architecture_preview.mp4");
const posterPng = path.join(outputDir, "02_Architecture_poster.png");

function run(command, args) {
  const result = spawnSync(command, args, { stdio: "inherit" });
  if (result.status !== 0) {
    throw new Error(`${command} failed with status ${result.status}`);
  }
}

function emptyDir(dir) {
  fs.rmSync(dir, { recursive: true, force: true });
  fs.mkdirSync(dir, { recursive: true });
}

async function main() {
  const { chromium } = await import("playwright");

  fs.mkdirSync(outputDir, { recursive: true });
  emptyDir(frameDir);

  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: WIDTH, height: HEIGHT }, deviceScaleFactor: 1 });
  await page.goto(`file://${path.join(__dirname, "index.html")}`);
  await page.waitForFunction(() => typeof window.renderFrame === "function");
  await page.evaluate(() => {
    window.__pauseAnimation = true;
  });

  for (let i = 0; i < FRAME_COUNT; i += 1) {
    const t = i / FPS;
    await page.evaluate((time) => window.renderFrame(time), t);
    await page.screenshot({ path: path.join(frameDir, `frame_${String(i).padStart(4, "0")}.png`) });
    if (i % 60 === 0) {
      console.log(`Rendered frame ${i}/${FRAME_COUNT}`);
    }
  }

  await page.evaluate(() => window.renderFrame(19.1));
  await page.screenshot({ path: posterPng });
  await browser.close();

  run(FFMPEG, [
    "-y",
    "-framerate",
    String(FPS),
    "-i",
    path.join(frameDir, "frame_%04d.png"),
    "-c:v",
    "libx264",
    "-pix_fmt",
    "yuv420p",
    "-movflags",
    "+faststart",
    "-crf",
    "18",
    fullMp4
  ]);

  run(FFMPEG, [
    "-y",
    "-i",
    fullMp4,
    "-vf",
    "scale=1280:-2",
    "-c:v",
    "libx264",
    "-pix_fmt",
    "yuv420p",
    "-movflags",
    "+faststart",
    "-crf",
    "24",
    previewMp4
  ]);

  run(FFPROBE, [
    "-v",
    "error",
    "-select_streams",
    "v:0",
    "-show_entries",
    "stream=width,height,codec_name,pix_fmt",
    "-of",
    "default=noprint_wrappers=1",
    fullMp4
  ]);
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
