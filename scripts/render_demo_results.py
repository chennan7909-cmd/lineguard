from __future__ import annotations

import json
import math
import shutil
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "results"
SUMMARY_PATH = RESULTS / "backtest_summary.json"
CHART_PATH = RESULTS / "decay_curve.png"
PNG_PATH = RESULTS / "demo_results.png"
MP4_PATH = RESULTS / "demo_results.mp4"
PLAYWRIGHT_FFMPEG_PATH = ROOT / "architecture" / ".ms-playwright" / "ffmpeg-1011" / "ffmpeg-mac"

WIDTH = 1920
HEIGHT = 1080
FPS = 30
DURATION_S = 8
FRAMES = FPS * DURATION_S

BG = (248, 250, 252)
INK = (21, 27, 37)
MUTED = (91, 104, 124)
SUBTLE = (219, 226, 235)
PANEL = (255, 255, 255)
RED = (183, 49, 67)
TEAL = (23, 119, 108)
BLUE = (45, 93, 171)


def font(name: str, size: int) -> ImageFont.FreeTypeFont:
    paths = {
        "regular": "/System/Library/Fonts/Supplemental/Arial.ttf",
        "bold": "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "mono": "/System/Library/Fonts/Supplemental/Courier New.ttf",
    }
    try:
        return ImageFont.truetype(paths[name], size)
    except OSError:
        return ImageFont.load_default(size=size)


F_TITLE = font("bold", 62)
F_KICKER = font("bold", 24)
F_CARD_VALUE = font("bold", 39)
F_CARD_LABEL = font("regular", 20)
F_SECTION = font("bold", 30)
F_BIG = font("bold", 68)
F_LABEL = font("bold", 24)
F_BODY = font("regular", 30)
F_SMALL = font("regular", 19)
F_DISCLAIMER = font("regular", 15)
F_MONO = font("mono", 18)


def load_values() -> dict:
    summary = json.loads(SUMMARY_PATH.read_text(encoding="utf-8"))
    sample = summary["sample"]
    all_signals = summary["results_all_signals"]
    return {
        "generated_by": summary["generated_by"],
        "matches": sample["matches"],
        "raw_rows": sample["odds_updates_total_rows"],
        "deduped": sample["odds_updates_1x2_deduped"],
        "signals": sample["signals"],
        "sharp": sample["signals_sharp"],
        "event": sample["signals_event_driven"],
        "lock_mean": all_signals["lock_immediate"]["mean"],
        "terminal_mean": all_signals["unhedged_terminal"]["mean"],
    }


def rounded_rect(draw: ImageDraw.ImageDraw, box, radius, fill, outline=None, width=1):
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


def text(draw, xy, s, fill, fnt, anchor=None):
    draw.text(xy, s, fill=fill, font=fnt, anchor=anchor)


def paste_alpha(base: Image.Image, layer: Image.Image, opacity: float):
    if opacity <= 0:
        return
    if opacity < 1:
        alpha = layer.getchannel("A").point(lambda p: int(p * opacity))
        layer = layer.copy()
        layer.putalpha(alpha)
    base.alpha_composite(layer)


def format_mean(value: float) -> str:
    return f"{value:.2f} per 100 stake"


def ascii_command(command: str) -> str:
    return command.replace("\u2013", "--").replace("\u2014", "--").replace("\u2212", "-")


def draw_metric_card(layer, x, y, w, h, value, label, accent):
    d = ImageDraw.Draw(layer)
    rounded_rect(d, (x, y, x + w, y + h), 8, PANEL, SUBTLE, 2)
    d.rectangle((x, y, x + 7, y + h), fill=accent)
    text(d, (x + 28, y + 29), value, INK, F_CARD_VALUE)
    text(d, (x + 28, y + 84), label, MUTED, F_CARD_LABEL)


def ease(t: float) -> float:
    t = max(0, min(1, t))
    return 1 - (1 - t) ** 3


def reveal(frame: int, start: int, span: int = 18) -> tuple[float, int]:
    p = ease((frame - start) / span)
    return p, int((1 - p) * 18)


def render_frame(values: dict, frame: int | None = None) -> Image.Image:
    final = frame is None
    frame = FRAMES - 1 if frame is None else frame
    img = Image.new("RGBA", (WIDTH, HEIGHT), BG + (255,))
    d = ImageDraw.Draw(img)

    d.rectangle((0, 0, WIDTH, 12), fill=TEAL)
    d.rectangle((0, 12, WIDTH, 18), fill=BLUE)

    title_layer = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    td = ImageDraw.Draw(title_layer)
    text(td, (82, 70), "CANONICAL BACKTEST RESULTS", INK, F_TITLE)
    text(td, (86, 148), "All figures below are read directly from results/backtest_summary.json", MUTED, F_SMALL)
    opacity, dy = (1, 0) if final else reveal(frame, 0, 18)
    if dy:
        shifted = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
        shifted.alpha_composite(title_layer, (0, dy))
        title_layer = shifted
    paste_alpha(img, title_layer, opacity)

    cards = [
        (f"{values['matches']:,}", "Matches", TEAL),
        (f"{values['raw_rows']:,}", "Raw Odds Rows", BLUE),
        (f"{values['deduped']:,}", "Deduped 1X2 Updates", TEAL),
        (f"{values['signals']:,}", "Signals", BLUE),
        (f"{values['sharp']:,} / {values['event']:,}", "Sharp / Event-Driven", TEAL),
    ]
    card_w, card_h, gap = 332, 132, 22
    for i, (value, label, accent) in enumerate(cards):
        layer = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
        opacity, dy = (1, 0) if final else reveal(frame, 15 + i * 9, 16)
        draw_metric_card(layer, 82 + i * (card_w + gap), 212 + dy, card_w, card_h, value, label, accent)
        paste_alpha(img, layer, opacity)

    comparison = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    cd = ImageDraw.Draw(comparison)
    rounded_rect(cd, (82, 402, 782, 820), 8, PANEL, SUBTLE, 2)
    text(cd, (122, 444), "RISK REDUCTION", TEAL, F_SECTION)
    text(cd, (122, 504), "Immediate Lock Mean", MUTED, F_LABEL)
    text(cd, (122, 543), format_mean(values["lock_mean"]), INK, F_BIG)
    text(cd, (122, 641), "Unhedged Terminal Mean", MUTED, F_LABEL)
    text(cd, (122, 680), format_mean(values["terminal_mean"]), RED, F_BIG)
    cd.line((122, 624, 742, 624), fill=SUBTLE, width=2)
    text(cd, (122, 756), "Earlier hedging materially reduces\nterminal downside.", INK, F_BODY)
    opacity, dy = (1, 0) if final else reveal(frame, 72, 22)
    if dy:
        shifted = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
        shifted.alpha_composite(comparison, (0, dy))
        comparison = shifted
    paste_alpha(img, comparison, opacity)

    chart_layer = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    chd = ImageDraw.Draw(chart_layer)
    rounded_rect(chd, (838, 402, 1838, 944), 8, PANEL, SUBTLE, 2)
    text(chd, (878, 444), "Existing chart: results/decay_curve.png", MUTED, F_LABEL)
    chart = Image.open(CHART_PATH).convert("RGBA")
    chart.thumbnail((920, 500), Image.Resampling.LANCZOS)
    chart_layer.alpha_composite(chart, (878 + (920 - chart.width) // 2, 492))
    opacity, dy = (1, 0) if final else reveal(frame, 98, 22)
    if dy:
        shifted = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
        shifted.alpha_composite(chart_layer, (0, dy))
        chart_layer = shifted
    paste_alpha(img, chart_layer, opacity)

    footer = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    fd = ImageDraw.Draw(footer)
    text(fd, (82, 972), ascii_command(values["generated_by"]), MUTED, F_MONO)
    text(fd, (82, 1004), "No positive-return claim. No smoothed or fabricated values.", (128, 139, 153), F_DISCLAIMER)
    paste_alpha(img, footer, 1 if final else ease((frame - 122) / 18))

    return img.convert("RGB")


def write_video(values: dict):
    ffmpeg_exe = shutil.which("ffmpeg")
    if ffmpeg_exe:
        ffmpeg_path = Path(ffmpeg_exe)
    else:
        try:
            import imageio_ffmpeg

            ffmpeg_path = Path(imageio_ffmpeg.get_ffmpeg_exe())
        except ImportError:
            ffmpeg_path = PLAYWRIGHT_FFMPEG_PATH
    if not ffmpeg_path.exists():
        raise SystemExit(f"Missing ffmpeg encoder: {ffmpeg_path}")
    cmd = [
        str(ffmpeg_path),
        "-y",
        "-f",
        "rawvideo",
        "-vcodec",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "-s",
        f"{WIDTH}x{HEIGHT}",
        "-r",
        str(FPS),
        "-i",
        "-",
        "-an",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        str(MP4_PATH),
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    assert proc.stdin is not None
    for frame in range(FRAMES):
        proc.stdin.write(render_frame(values, frame).tobytes())
    proc.stdin.close()
    code = proc.wait()
    if code:
        raise SystemExit(f"ffmpeg failed with exit code {code}")


def main():
    values = load_values()
    if not CHART_PATH.exists():
        raise SystemExit(f"Missing chart: {CHART_PATH}")
    PNG_PATH.parent.mkdir(exist_ok=True)
    render_frame(values).save(PNG_PATH)
    write_video(values)
    print(f"wrote {PNG_PATH}")
    print(f"wrote {MP4_PATH}")
    print(f"{WIDTH}x{HEIGHT}, {FPS} fps, {DURATION_S} seconds, no audio")


if __name__ == "__main__":
    main()
