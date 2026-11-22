"""Build the demo cast, transcript page, and GIF from the offline fixture session.

Runs ``scripts/demo.py``, which drives the real CLI and MCP server against
``tests/fake_benchling.py`` with ``httpx.MockTransport``. No credentials and no
network. The asciinema v2 cast is that transcript with scripted timing. The GIF
is a drawing of the same transcript. Neither file is a recording of a live
terminal or a live tenant.

    uv run --with pillow python scripts/render_demo.py

Pillow and ffmpeg are used only to draw the GIF. They are not package
dependencies. ``--skip-gif`` writes the cast and refreshes the transcript in docs/demo.md.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "scripts" / "demo.py"
CAST_PATH = ROOT / "docs" / "demo.cast"
GIF_PATH = ROOT / "docs" / "demo.gif"
DEMO_DOC_PATH = ROOT / "docs" / "demo.md"
TRANSCRIPT_START = "<!-- demo-transcript:start -->"
TRANSCRIPT_END = "<!-- demo-transcript:end -->"

INTRO = "$ uv run python scripts/demo.py"
WIDTH_FLOOR = 80
ROWS = 24
CAST_TIMESTAMP = 1763580000

FONT = Path("/usr/share/fonts/truetype/jetbrains-mono/JetBrainsMono-Regular.ttf")
BG = (28, 31, 38)
BAR = (18, 20, 26)
FG = (230, 230, 230)
COMMENT = (122, 162, 247)
PROMPT = (158, 206, 106)
EXIT_OK = (158, 206, 106)
EXIT_BAD = (247, 118, 142)
NOTE = (224, 175, 104)
MUTED = (140, 146, 160)

_FORBIDDEN = ("client_secret", "access_token", "demo-client-secret", "Authorization")
_URL = re.compile(r"https://[^\s`\"']+")
_ALLOWED_URL = re.compile(r"https://your-tenant(?:-dev)?\.benchling\.com(?:/[^\s`\"']*)?")


def capture_demo() -> str:
    """Run the fixture demo and return its stdout."""
    completed = subprocess.run(
        [sys.executable, str(DEMO)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    if completed.stderr.strip():
        raise SystemExit(f"scripts/demo.py wrote to stderr:\n{completed.stderr}")
    return completed.stdout


def assert_fixture_text(text: str) -> None:
    """Refuse to publish a transcript that is not the offline fixture session."""
    required = (
        "remote writes so far: 0",
        "remote writes in total: 1",
        "Entry version changed after the plan was created",
        "Plan has already been applied or attempted",
        "MCP writes are disabled for demo",
        "etr_abc123",
        "[exit 0]",
        "[exit 5]",
    )
    missing = [item for item in required if item not in text]
    if missing:
        raise SystemExit(f"fixture transcript is missing: {', '.join(missing)}")
    found = [item for item in _FORBIDDEN if item in text]
    if found:
        raise SystemExit(f"fixture transcript contains forbidden text: {', '.join(found)}")
    for url in _URL.findall(text):
        if _ALLOWED_URL.fullmatch(url) is None:
            raise SystemExit(f"fixture transcript contains a non-placeholder URL: {url}")


def build_cast(transcript: str) -> str:
    """Return an asciinema v2 cast of the fixture transcript."""
    width = max(WIDTH_FLOOR, *(len(line) for line in transcript.splitlines()), len(INTRO))
    header = {
        "version": 2,
        "width": width,
        "height": ROWS,
        "timestamp": CAST_TIMESTAMP,
        "title": "benchling-tools fixture demo",
        "env": {"TERM": "dumb", "SHELL": "/bin/sh"},
    }
    lines = [json.dumps(header, separators=(",", ":"))]
    clock = 0.0
    for delay, text, _color in scripted_events(transcript):
        clock += delay
        if text:
            lines.append(json.dumps([round(clock, 3), "o", text], separators=(",", ":")))
    return "\n".join(lines) + "\n"


def scripted_events(transcript: str) -> list[tuple[float, str, str]]:
    """Pauses, text, and color for one scripted playback.

    The delay is how long the frame stays after the text is shown. The cast
    uses the text and ignores the color.
    """
    events: list[tuple[float, str, str]] = []
    for character in INTRO:
        events.append((0.02, character, "prompt"))
    events.append((0.5, "\n", "prompt"))
    for paragraph in transcript.split("\n\n"):
        body = paragraph.split("\n")
        for index, line in enumerate(body):
            last = index == len(body) - 1
            if line.startswith("# "):
                delay = 0.7
            elif line.startswith("$ "):
                delay = 0.45
            elif line.startswith("[exit ") or line.startswith("remote writes"):
                delay = 0.9
            elif last:
                delay = 1.1
            else:
                delay = 0.03
            events.append((delay, line + "\n", _color_of(line)))
        events.append((0.15, "\n", "fg"))
    events.append((1.6, "", "fg"))
    return events


def refresh_transcript(transcript: str) -> None:
    """Replace the fenced transcript between the markers in docs/demo.md."""
    page = DEMO_DOC_PATH.read_text(encoding="utf-8")
    if TRANSCRIPT_START not in page or TRANSCRIPT_END not in page:
        raise SystemExit("docs/demo.md is missing demo-transcript markers")
    fenced = "```console\n" + transcript.rstrip() + "\n```"
    pattern = re.compile(
        re.escape(TRANSCRIPT_START) + r"\n.*?" + re.escape(TRANSCRIPT_END),
        re.DOTALL,
    )
    replacement = f"{TRANSCRIPT_START}\n{fenced}\n{TRANSCRIPT_END}"
    updated, count = pattern.subn(replacement, page, count=1)
    if count != 1:
        raise SystemExit("docs/demo.md demo-transcript markers were not replaced")
    DEMO_DOC_PATH.write_text(updated, encoding="utf-8")


def render_gif(transcript: str, destination: Path) -> None:
    """Draw the scripted session and encode it with ffmpeg."""
    from PIL import ImageFont

    if not FONT.is_file():
        raise SystemExit(f"monospace font not found: {FONT}")
    if shutil.which("ffmpeg") is None:
        raise SystemExit("ffmpeg is required to encode the GIF")
    font = ImageFont.truetype(str(FONT), 15)
    cell = max(1, round(font.getlength("0")))
    line_height = 20
    width = max(WIDTH_FLOOR, *(len(line) for line in transcript.splitlines()), len(INTRO))
    pad_x, pad_y, bar_h = 16, 12, 32
    image_w = pad_x * 2 + width * cell
    image_h = bar_h + pad_y * 2 + ROWS * line_height
    term = _Terminal(width, ROWS)

    with tempfile.TemporaryDirectory(prefix="benchling-demo-") as temporary:
        root = Path(temporary)
        manifest = root / "frames.txt"
        entries: list[str] = []
        index = 0
        for delay, text, color in scripted_events(transcript):
            if text:
                term.write(text, color)
            frame = root / f"frame-{index:04d}.png"
            _draw_frame(frame, term, font, cell, line_height, image_w, image_h, pad_x, pad_y, bar_h)
            entries.append(f"file '{frame.name}'\nduration {delay:.3f}\n")
            index += 1
        if not entries:
            raise SystemExit("no frames to encode")
        entries.append(f"file 'frame-{index - 1:04d}.png'\n")
        manifest.write_text("".join(entries), encoding="utf-8")
        palette = root / "palette.png"
        _ffmpeg(
            [
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(manifest),
                "-vf",
                "palettegen=stats_mode=diff",
                str(palette),
            ]
        )
        _ffmpeg(
            [
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(manifest),
                "-i",
                str(palette),
                "-lavfi",
                "paletteuse=dither=bayer:bayer_scale=3",
                "-loop",
                "0",
                str(destination),
            ]
        )


class _Terminal:
    """A wrapping, scrolling character grid with one color per span."""

    def __init__(self, cols: int, rows: int) -> None:
        self.cols = cols
        self.rows = rows
        self.lines: list[list[tuple[str, str]]] = [[]]

    def write(self, text: str, color: str) -> None:
        for character in text:
            if character == "\r":
                continue
            if character == "\n":
                self.lines.append([])
                self._trim()
                continue
            if self._width(self.lines[-1]) >= self.cols:
                self.lines.append([])
                self._trim()
            spans = self.lines[-1]
            if spans and spans[-1][1] == color:
                spans[-1] = (spans[-1][0] + character, color)
            else:
                spans.append((character, color))

    def snapshot(self) -> list[list[tuple[str, str]]]:
        visible = self.lines[-self.rows :]
        return [list(line) for line in visible]

    def _width(self, spans: list[tuple[str, str]]) -> int:
        return sum(len(text) for text, _color in spans)

    def _trim(self) -> None:
        if len(self.lines) > self.rows * 4:
            self.lines = self.lines[-self.rows :]


def _color_of(line: str) -> str:
    if line.startswith("# "):
        return "comment"
    if line.startswith("$ ") or line.startswith("changes_apply("):
        return "prompt"
    if line.startswith("[exit 0]"):
        return "ok"
    if line.startswith("[exit "):
        return "bad"
    if line.startswith("remote writes"):
        return "note"
    return "fg"


def _draw_frame(
    path: Path,
    term: _Terminal,
    font: object,
    cell: int,
    line_height: int,
    image_w: int,
    image_h: int,
    pad_x: int,
    pad_y: int,
    bar_h: int,
) -> None:
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (image_w, image_h), BG)
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, image_w, bar_h), fill=BAR)
    for offset, color in enumerate(((247, 118, 142), (224, 175, 104), (158, 206, 106))):
        draw.ellipse((14 + offset * 18, 10, 26 + offset * 18, 22), fill=color)
    draw.text((78, 7), "benchling-tools    fixture demo    no network", font=font, fill=MUTED)
    colors = {
        "fg": FG,
        "comment": COMMENT,
        "prompt": PROMPT,
        "ok": EXIT_OK,
        "bad": EXIT_BAD,
        "note": NOTE,
    }
    top = bar_h + pad_y
    for row, spans in enumerate(term.snapshot()):
        x = pad_x
        y = top + row * line_height
        for text, name in spans:
            draw.text((x, y), text, font=font, fill=colors[name])
            x += len(text) * cell
    image.save(path, format="PNG")


def _ffmpeg(args: list[str]) -> None:
    completed = subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", *args],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise SystemExit(completed.stderr.strip() or "ffmpeg failed")


def main() -> None:
    """Capture the fixture demo and write docs/demo.cast, docs/demo.md, and the GIF."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--skip-gif", action="store_true", help="Write the cast and docs/demo.md only."
    )
    args = parser.parse_args()
    transcript = capture_demo()
    assert_fixture_text(transcript)
    CAST_PATH.write_text(build_cast(transcript), encoding="utf-8")
    refresh_transcript(transcript)
    if args.skip_gif:
        return
    render_gif(transcript, GIF_PATH)


if __name__ == "__main__":
    main()
