#!/usr/bin/env python3
"""Replay asciinema .cast file → PNG terminal screenshot.

Renders the final screen buffer with one matplotlib Text per color-run
(not per character) so monospace alignment is preserved.
"""
import json, sys
import pyte
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.font_manager import FontProperties

cast_path  = sys.argv[1] if len(sys.argv) > 1 else "pipeline_demo.cast"
out_path   = sys.argv[2] if len(sys.argv) > 2 else "images/terminal.png"
last_rows  = int(sys.argv[3]) if len(sys.argv) > 3 else 38

with open(cast_path) as f:
    header = json.loads(f.readline())
    cols, rows = header["width"], 80
    screen = pyte.Screen(cols, rows)
    stream = pyte.ByteStream(screen)
    for line in f:
        try:
            t, kind, data = json.loads(line)
        except Exception:
            continue
        if kind == "o":
            stream.feed(data.encode("utf-8", errors="replace"))

# Collect non-empty lines from buffer top to bottom
text_rows = []
for line_idx in range(rows):
    row_chars = screen.buffer[line_idx]
    chars = []
    for i in range(cols):
        c = row_chars[i]
        chars.append((c.data, c.fg, c.bold))
    text_rows.append(chars)

# Trim trailing blanks
while text_rows and "".join(c[0] for c in text_rows[-1]).strip() == "":
    text_rows.pop()
if last_rows and len(text_rows) > last_rows:
    text_rows = text_rows[-last_rows:]

COLOR_MAP = {
    "default":  "#dcdcdc",
    "black":    "#000000",
    "red":      "#ff6b6b",
    "green":    "#5ed77b",
    "yellow":   "#f0d75a",
    "blue":     "#5fa8d3",
    "magenta":  "#d96cc6",
    "cyan":     "#6cd5d5",
    "white":    "#ffffff",
    "brown":    "#bb9966",
}

# Group each row into (color, bold, start_col, text) runs
def runs_for_row(row):
    out = []
    cur_color = None
    cur_bold = None
    cur_start = 0
    cur_txt = ""
    for i, (ch, fg, bold) in enumerate(row):
        color_key = (fg, bold)
        if color_key != (cur_color, cur_bold):
            if cur_txt:
                out.append((cur_start, cur_txt, cur_color, cur_bold))
            cur_start, cur_txt = i, ch
            cur_color, cur_bold = fg, bold
        else:
            cur_txt += ch
    if cur_txt:
        out.append((cur_start, cur_txt, cur_color, cur_bold))
    return out

# Render
nrows = len(text_rows)
char_w = 0.085          # inches per char (≈ 8pt monospace)
char_h = 0.16
fig_w = max(8, cols * char_w + 0.8)
fig_h = max(3, (nrows + 2) * char_h + 0.6)
fig, ax = plt.subplots(figsize=(fig_w, fig_h), dpi=150)
ax.set_facecolor("#1e1e1e")
fig.patch.set_facecolor("#1e1e1e")
ax.set_xlim(0, cols)
ax.set_ylim(0, nrows + 1)
ax.invert_yaxis()
ax.axis("off")

# macOS-style title bar
ax.add_patch(mpatches.Rectangle((0, -0.9), cols, 0.9,
                                facecolor="#2d2d2d", edgecolor="none"))
for i, c in enumerate(["#ff5f56", "#ffbd2e", "#27c93f"]):
    ax.add_patch(mpatches.Circle((1.3 + i*1.6, -0.45), 0.32,
                                 facecolor=c, edgecolor="none"))
ax.text(cols/2, -0.45, "aria — self-test pipeline",
        color="#bbb", ha="center", va="center", fontsize=9,
        family="monospace")

mono = FontProperties(family=["DejaVu Sans Mono", "Consolas", "monospace"],
                      size=9.5)
mono_b = FontProperties(family=["DejaVu Sans Mono", "Consolas", "monospace"],
                        size=9.5, weight="bold")

for y, row in enumerate(text_rows):
    for start, txt, fg, bold in runs_for_row(row):
        if not txt.strip():
            continue
        ax.text(start, y + 0.5, txt,
                color=COLOR_MAP.get(fg, "#dcdcdc"),
                fontproperties=mono_b if bold else mono,
                ha="left", va="center")

ax.set_ylim(nrows + 0.5, -1.0)
plt.savefig(out_path, dpi=150, bbox_inches="tight",
            facecolor="#1e1e1e", pad_inches=0.15)
plt.close()
print(f"OK  {out_path}  ({nrows} rows × {cols} cols)")
