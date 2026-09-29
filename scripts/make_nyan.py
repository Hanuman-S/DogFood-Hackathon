"""Draw Nyan Cat as pixel-art SVG and write it to src/templates/_nyan.html.

    python scripts/make_nyan.py

The sprite is built from the maps below, one character per pixel, and written as <rect> runs
(neighbouring pixels of one colour merged), so the page carries no image file and no script. The
parts are grouped (rainbow segments, tail, body, legs, stars) so crt.css can animate them with
plain CSS transforms: the rainbow waves, the cat bobs, the legs paddle, the stars stream past.
No inline style attributes (the CSP forbids them): colours are SVG presentation attributes.
"""

from collections import defaultdict
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "src" / "templates" / "_nyan.html"

COLOURS = {
    "K": "#000000",  # outline
    "C": "#ffcc99",  # crust
    "P": "#ff99ff",  # frosting
    "S": "#ff3399",  # sprinkles
    "G": "#999999",  # fur
    "W": "#ffffff",  # eye shine, stars
    "R": "#ff9999",  # cheeks
}
RAINBOW = ["#ff0000", "#ff9900", "#ffff00", "#33ff00", "#0099ff", "#6633ff"]

WIDTH, HEIGHT = 80, 31
TOP = 6            # the pop-tart's top row
POP_X = 44         # the pop-tart's left column
SEGMENT = 8        # rainbow segment width: alternate segments wave out of step
# The rainbow (and the stars) go on this far left of the picture, past its left edge: the page
# shows the SVG overflowing and cuts it at the content column's edge, so on any screen the rainbow
# starts exactly where the page's padding does.
REACH = 320
BAND = 3           # rainbow band height

HEAD = [
    "..KK........KK..",
    ".KGGK......KGGK.",
    ".KGGGK....KGGGK.",
    ".KGGGGKKKKGGGGK.",
    "KGGGGGGGGGGGGGGK",
    "KGGGWKGGGGGWKGGK",
    "KGGGKKGGGKGKKGGK",
    "KGRRGGGGGGGGGRRK",
    "KGRRGKGGKGGKGRRK",
    "KGGGGGKKGKKGGGGK",
    ".KGGGGGGGGGGGGK.",
    "..KKKKKKKKKKKK..",
]
TAIL = [
    "KK...",
    "KGKK.",
    ".KGGK",
    "..KGK",
    "...KK",
]
LEG = [
    "KGGK",
    "KGGK",
    ".KK.",
]
STAR = [
    "..W..",
    "..W..",
    "WW.WW",
    "..W..",
    "..W..",
]
# (x, y) of each star's top-left; the set repeats every WIDTH, from -REACH on, so it can loop.
STARS = [(2, 0), (22, 2), (36, 0), (12, 26), (30, 25), (58, 1), (70, 26), (48, 26)]
SPRINKLES = [(5, 4), (9, 3), (14, 4), (4, 8), (8, 7), (11, 10), (5, 12), (9, 14), (13, 13)]


def stamp(grid, rows, x0, y0):
    for dy, row in enumerate(rows):
        for dx, ch in enumerate(row):
            if ch != ".":
                grid[(x0 + dx, y0 + dy)] = COLOURS[ch]


def pop_tart():
    grid = {}
    w, h = 21, 18
    for x in range(w):
        for y in range(h):
            corner = x in (0, w - 1) and y in (0, h - 1)
            if corner:
                continue
            inner_corner = x in (1, w - 2) and y in (1, h - 2)
            if x in (0, w - 1) or y in (0, h - 1) or inner_corner:
                colour = COLOURS["K"]
            elif 3 <= x <= w - 4 and 3 <= y <= h - 4 and not (x in (3, w - 4) and y in (3, h - 4)):
                colour = COLOURS["P"]
            else:
                colour = COLOURS["C"]
            grid[(POP_X + x, TOP + y)] = colour
    for sx, sy in SPRINKLES:
        grid[(POP_X + sx, TOP + sy)] = COLOURS["S"]
    return grid


def rainbow(parity):
    """The segments whose index has this parity (0 or 1)."""
    grid = {}
    for x in range(-REACH, POP_X + 2):
        if (x // SEGMENT) % 2 != parity:
            continue
        for band, colour in enumerate(RAINBOW):
            for dy in range(BAND):
                grid[(x, TOP + 1 + band * BAND + dy)] = colour
    return grid


def rects(grid):
    """Merge horizontal runs of one colour into <rect>s."""
    rows = defaultdict(list)
    for (x, y), colour in grid.items():
        rows[y].append((x, colour))
    out = []
    for y in sorted(rows):
        run = sorted(rows[y])
        start, prev, colour = run[0][0], run[0][0], run[0][1]
        for x, c in run[1:] + [(None, None)]:
            if x == prev + 1 and c == colour:
                prev = x
                continue
            out.append(f'<rect x="{start}" y="{y}" width="{prev - start + 1}" height="1" fill="{colour}"/>')
            if x is not None:
                start, prev, colour = x, x, c
    return "".join(out)


def main():
    tail, legs, head, stars = {}, {}, {}, {}
    stamp(tail, TAIL, POP_X - 5, TOP + 8)
    for lx in (1, 5, 12, 16):
        stamp(legs, LEG, POP_X + lx, TOP + 17)
    stamp(head, HEAD, POP_X + 12, TOP + 5)
    for sx, sy in STARS:
        for copy in range(-REACH, 2 * WIDTH, WIDTH):
            stamp(stars, STAR, sx + copy, sy)

    body = pop_tart()
    body.update(head)
    svg = (
        '{# Nyan Cat, pixel art drawn by scripts/make_nyan.py (edit the maps there, then rerun). '
        'Animated by crt.css (.nyan). #}\n'
        f'<svg class="nyan" viewBox="0 0 {WIDTH} {HEIGHT}" role="img" aria-label="Nyan Cat, flying" '
        'shape-rendering="crispEdges">'
        f'<g class="nyan__stars">{rects(stars)}</g>'
        f'<g class="nyan__rainbow nyan__rainbow--a">{rects(rainbow(0))}</g>'
        f'<g class="nyan__rainbow nyan__rainbow--b">{rects(rainbow(1))}</g>'
        f'<g class="nyan__legs">{rects(legs)}</g>'
        f'<g class="nyan__body">{rects(tail)}{rects(body)}</g>'
        "</svg>\n"
    )
    OUT.write_text(svg, encoding="utf-8", newline="\n")
    print(f"wrote {OUT} ({len(svg)} bytes)")


if __name__ == "__main__":
    main()
