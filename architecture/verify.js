import { spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const FFPROBE = "/opt/anaconda3/envs/lineguard-video/bin/ffprobe";
const required = [
  { file: "output/02_Architecture.mp4", minBytes: 500000, width: 1920, height: 1080 },
  { file: "output/02_Architecture_preview.mp4", minBytes: 150000, width: 1280 },
  { file: "output/02_Architecture_poster.png", minBytes: 100000 }
];

function probeVideo(filePath) {
  const result = spawnSync(
    FFPROBE,
    [
      "-v",
      "error",
      "-select_streams",
      "v:0",
      "-show_entries",
      "stream=width,height,codec_name,pix_fmt,duration",
      "-of",
      "json",
      filePath
    ],
    { encoding: "utf8" }
  );
  if (result.status !== 0) {
    throw new Error(`ffprobe failed for ${filePath}: ${result.stderr}`);
  }
  return JSON.parse(result.stdout).streams[0];
}

for (const item of required) {
  const filePath = path.join(__dirname, item.file);
  if (!fs.existsSync(filePath)) {
    throw new Error(`Missing ${item.file}`);
  }
  const size = fs.statSync(filePath).size;
  if (size < item.minBytes) {
    throw new Error(`${item.file} is unexpectedly small: ${size} bytes`);
  }

  if (item.file.endsWith(".mp4")) {
    const stream = probeVideo(filePath);
    if (stream.width !== item.width) {
      throw new Error(`${item.file} width ${stream.width} did not match ${item.width}`);
    }
    if (item.height && stream.height !== item.height) {
      throw new Error(`${item.file} height ${stream.height} did not match ${item.height}`);
    }
    console.log(`${item.file}: ${stream.codec_name}, ${stream.width}x${stream.height}, ${stream.pix_fmt}`);
  } else {
    console.log(`${item.file}: ${size} bytes`);
  }
}

console.log("Architecture animation outputs verified.");
