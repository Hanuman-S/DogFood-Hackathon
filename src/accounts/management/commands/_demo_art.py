"""Pictures for the demo projects: a 16:9 thumbnail and two screenshots each, drawn here with Pillow.

Nothing is downloaded (the portal works offline) and nothing is random: the same project always
gets the same bytes. Everything is drawn on a small pixel grid and scaled up with nearest-neighbour,
so it matches the portal's pixel look. seed_demo stores them through projects.images.clean_image,
like any upload.
"""

from io import BytesIO

from PIL import Image, ImageDraw

W, H = 96, 54          # the drawing grid (16:9)
SCALE = 10             # 960 x 540 on disk

BG = (13, 11, 24)
GRID = (26, 22, 48)
INK = (239, 235, 255)
DIM = (139, 131, 201)
FAINT = (77, 70, 128)
GOOD, BAD, WARN = (80, 220, 120), (255, 110, 150), (255, 169, 77)

# 16 x 16 icons, one character per pixel: "#" the project's colour, "o" white, "." nothing.
ICONS = {
    "moon": [
        "......####......", "....###.........", "...###..........", "..###...........",
        "..###.......o...", ".###........o.o.", ".###.........o..", ".###............",
        ".###............", ".####...........", "..####......####", "..######..######",
        "...############.", "....##########..", "......######....", "................",
    ],
    "pin": [
        ".....######.....", "....########....", "...###....###...", "...##..oo..##...",
        "...##..oo..##...", "...###....###...", "....########....", ".....######.....",
        "......####......", "......####......", ".......##.......", ".......##.......",
        "................", "....oooooooo....", "...oooooooooo...", "................",
    ],
    "robot": [
        ".......##.......", ".......##.......", "...##########...", "..############..",
        "..##oo####oo##..", "..##oo####oo##..", "..############..", "..############..",
        "..###oooooo###..", "..############..", "...##########...", ".....######.....",
        "..############..", ".##############.", ".##.########.##.", ".##.########.##.",
    ],
    "lamp": [
        "....########....", "...##oooooo##...", "..##oooooooo##..", "..############..",
        "......####......", ".......##.......", ".......##.......", ".......##.......",
        ".......##.......", ".......##.......", ".......##.......", ".......##.......",
        ".......##.......", "......####......", ".....######.....", "...##########...",
    ],
    "hat": [
        "................", "................", "....########....", "....########....",
        "....########....", "....########....", "....########....", "....########....",
        "....oooooooo....", "....oooooooo....", "....########....", "..############..",
        ".##############.", "................", "................", "................",
    ],
    "bell": [
        ".......##......o", "......####....o.", ".....######..o..", "....########o...",
        "....#######o##..", "....######o###..", "....#####o####..", "...#####o######.",
        "...####o#######.", "..####o#########", "..###o##########", "..##o###########",
        ".oo#############", "o.....####......", "......####......", ".......##.......",
    ],
}

PROJECTS = {
    "Sleep Debt": {"icon": "moon", "colour": (140, 120, 255), "screens": ["chart", "list"],
                   "captions": ["hours slept per night, against what a hackathon costs",
                                "the debt, event by event"]},
    "Quiet Map": {"icon": "pin", "colour": (80, 200, 255), "screens": ["map", "list"],
                  "captions": ["the venue, loudest tables in red", "tables ranked by noise"]},
    "Stand-up Bot": {"icon": "robot", "colour": (120, 230, 160), "screens": ["chat", "list"],
                     "captions": ["the bot collecting today's stand-up", "the summary it posts"]},
    "Lamp Post": {"icon": "lamp", "colour": (255, 200, 90), "screens": ["status", "chart"],
                  "captions": ["Wi-Fi status by room", "speed over the day"]},
    "Pairing Hat": {"icon": "hat", "colour": (255, 130, 200), "screens": ["pairs", "list"],
                    "captions": ["suggested pairs, from the skills people list", "everyone's skills"]},
    "Quiet Hours": {"icon": "bell", "colour": (255, 169, 77), "screens": ["toggles", "chart"],
                    "captions": ["focus mode and the apps it mutes", "notifications held back per hour"]},
}


def _canvas():
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    for x in range(0, W, 6):
        d.line([(x, 0), (x, H)], fill=GRID)
    for y in range(0, H, 6):
        d.line([(0, y), (W, y)], fill=GRID)
    return img, d


def _icon(d, name, colour, x0, y0, scale=1):
    for dy, row in enumerate(ICONS[name]):
        for dx, ch in enumerate(row):
            if ch != ".":
                fill = colour if ch == "#" else INK
                d.rectangle([x0 + dx * scale, y0 + dy * scale, x0 + dx * scale + scale - 1,
                             y0 + dy * scale + scale - 1], fill=fill)


def _png(img):
    out = BytesIO()
    img.resize((W * SCALE, H * SCALE), Image.NEAREST).save(out, "PNG", optimize=True)
    return out.getvalue()


def thumbnail(project_name):
    spec = PROJECTS[project_name]
    img, d = _canvas()
    c = spec["colour"]
    # A soft band of the project's colour behind a large icon, and a few "text" lines beside it.
    d.rectangle([0, 34, W, 42], fill=tuple(v // 5 for v in c))
    _icon(d, spec["icon"], c, 10, 7, scale=2)
    for i, width in enumerate((40, 30, 34)):
        d.rectangle([50, 14 + i * 7, 50 + width, 16 + i * 7], fill=INK if i == 0 else DIM)
    d.rectangle([50, 38, 72, 43], outline=c)
    return _png(img)


def _window(d, colour):
    d.rectangle([4, 3, W - 5, H - 4], fill=(18, 15, 34), outline=FAINT)
    d.rectangle([4, 3, W - 5, 8], fill=tuple(v // 3 for v in colour))
    for i, dot in enumerate((BAD, WARN, GOOD)):
        d.rectangle([7 + i * 4, 5, 8 + i * 4, 6], fill=dot)


def _screen(kind, colour):
    img, d = _canvas()
    _window(d, colour)
    if kind == "chart":
        for i, h in enumerate((18, 26, 12, 30, 22, 8, 16, 28, 20, 24)):
            d.rectangle([10 + i * 8, 46 - h, 14 + i * 8, 46], fill=colour if h > 15 else FAINT)
        d.line([(9, 47), (W - 9, 47)], fill=DIM)
    elif kind == "list":
        for i in range(5):
            y = 13 + i * 7
            d.rectangle([10, y, 13, y + 3], fill=colour)
            d.rectangle([17, y, 17 + (50, 38, 44, 30, 40)[i], y + 2], fill=INK if i == 0 else DIM)
            d.rectangle([W - 22, y, W - 12, y + 2], fill=(GOOD, WARN, GOOD, BAD, GOOD)[i])
    elif kind == "map":
        for x, y, w, h in ((10, 12, 22, 14), (36, 12, 20, 14), (60, 12, 24, 30), (10, 30, 46, 14)):
            d.rectangle([x, y, x + w, y + h], outline=DIM)
        for x, y, c in ((16, 17, GOOD), (26, 20, BAD), (44, 17, GOOD), (70, 20, WARN), (70, 34, GOOD),
                        (20, 36, BAD), (40, 38, GOOD)):
            d.rectangle([x, y, x + 3, y + 3], fill=c)
        _icon(d, "pin", colour, 40, 22)
    elif kind == "chat":
        for i, (side, width) in enumerate((("l", 44), ("r", 30), ("l", 52), ("r", 36))):
            y = 12 + i * 9
            x = 10 if side == "l" else W - 12 - width
            d.rectangle([x, y, x + width, y + 5], fill=colour if side == "l" else FAINT)
    elif kind == "status":
        for i in range(4):
            y = 13 + i * 9
            d.rectangle([10, y, 44, y + 5], outline=DIM)
            d.rectangle([12, y + 2, 14, y + 3], fill=(GOOD, GOOD, BAD, WARN)[i])
            d.rectangle([50, y + 1, 50 + (34, 30, 8, 18)[i], y + 4], fill=(GOOD, GOOD, BAD, WARN)[i])
    elif kind == "pairs":
        for i in range(3):
            y = 13 + i * 12
            for x in (14, 58):
                d.rectangle([x, y, x + 8, y + 8], fill=colour)
                d.rectangle([x + 2, y + 2, x + 3, y + 3], fill=INK)
                d.rectangle([x + 5, y + 2, x + 6, y + 3], fill=INK)
            d.line([(26, y + 4), (54, y + 4)], fill=DIM)
    elif kind == "toggles":
        for i in range(5):
            y = 13 + i * 7
            on = i in (0, 1, 3)
            d.rectangle([10, y, 50, y + 2], fill=DIM)
            d.rectangle([W - 24, y - 1, W - 12, y + 3], outline=colour if on else FAINT)
            knob = W - 17 if on else W - 23
            d.rectangle([knob, y, knob + 4, y + 2], fill=colour if on else FAINT)
    return _png(img)


def screenshots(project_name):
    """[(png bytes, caption)] for the project's image gallery."""
    spec = PROJECTS[project_name]
    return [(_screen(kind, spec["colour"]), caption) for kind, caption in zip(spec["screens"], spec["captions"])]
