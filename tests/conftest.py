"""Shared fixtures: synthetic memory cards, save files and 3D icons."""

import io
import struct

import pytest

from mymc import ps2mc, ps2save
from mymc.ps2mc_dir import (
    DF_0400,
    DF_DIR,
    DF_EXISTS,
    DF_FILE,
    DF_RWX,
    tod_now,
)

STANDARD_PARAMS = (
    True,
    ps2mc.PS2MC_STANDARD_PAGE_SIZE,
    ps2mc.PS2MC_STANDARD_PAGES_PER_ERASE_BLOCK,
    ps2mc.PS2MC_STANDARD_PAGES_PER_CARD,
)

FIXED = 4096


def make_icon_sys(line1="UNLIMITED SAGA", line2="SYSTEMDATA", icon=b"list.icn"):
    """Build a valid 964 byte icon.sys."""
    title = (line1 + line2).encode("shift_jis")
    return struct.pack(
        "<4s2xH4xL16s16s16s16s16s16s16s16s16s16s16s68s64s64s64s512x",
        b"PS2D",
        len(line1.encode("shift_jis")),
        0,
        *[struct.pack("<4L", 0x40, 0x40, 0x40, 0)] * 4,
        struct.pack("<4f", 1, -1, 2, 0),
        struct.pack("<4f", -1, 1, -2, 0),
        struct.pack("<4f", 0, 1, 0, 0),
        struct.pack("<4f", 1, 1, 1, 1),
        struct.pack("<4f", 1, 1, 1, 1),
        struct.pack("<4f", 0.7, 0.7, 0.7, 1),
        struct.pack("<4f", 0.5, 0.5, 0.5, 1),
        title,
        icon,
        icon,
        icon,
    )


def make_icn(shapes=2, compressed=False):
    """Build a small but structurally valid .icn file: a textured cube."""
    corners = [
        (-1, -1, -1), (1, -1, -1), (1, 1, -1), (-1, 1, -1),
        (-1, -1, 1), (1, -1, 1), (1, 1, 1), (-1, 1, 1),
    ]
    faces = [
        ((0, 1, 2, 3), (0, 0, -1)), ((5, 4, 7, 6), (0, 0, 1)),
        ((4, 0, 3, 7), (-1, 0, 0)), ((1, 5, 6, 2), (1, 0, 0)),
        ((4, 5, 1, 0), (0, -1, 0)), ((3, 2, 6, 7), (0, 1, 0)),
    ]
    colours = [
        (255, 80, 80), (80, 255, 80), (80, 80, 255),
        (255, 255, 80), (255, 80, 255), (80, 255, 255),
    ]
    uv_quad = [(0, 0), (1, 0), (1, 1), (0, 1)]

    verts = []
    for fi, (quad, normal) in enumerate(faces):
        for tri in ((0, 1, 2), (0, 2, 3)):
            for k in tri:
                verts.append((corners[quad[k]], normal, uv_quad[k], colours[fi]))

    out = bytearray()
    tex_type = 0x0F if compressed else 0x07
    out += struct.pack("<5L", 0x00010000, shapes, tex_type, 0x3F800000, len(verts))
    for (pos, normal, uv, colour) in verts:
        for s in range(shapes):
            scale = 1.0 + 0.35 * s
            for a in pos:
                out += struct.pack("<h", int(a * scale * FIXED / 2))
            out += struct.pack("<h", 0)
        for a in normal:
            out += struct.pack("<h", int(a * FIXED))
        out += struct.pack("<h", 0)
        out += struct.pack(
            "<hh", int(uv[0] * FIXED * 0.999), int(uv[1] * FIXED * 0.999)
        )
        out += bytes((colour[0], colour[1], colour[2], 0x80))

    out += struct.pack("<LLfLL", 1, 60, 1.0, 0, 1)
    out += struct.pack("<LL", 0, 0)

    # 128x128 RGBA5551 checkerboard
    pixels = []
    for y in range(128):
        for x in range(128):
            checker = ((x // 16) + (y // 16)) % 2
            r = 31 if checker else x * 31 // 127
            g = y * 31 // 127 if checker else 31
            b = 8 if checker else 24
            pixels.append(r | (g << 5) | (b << 10) | 0x8000)
    raw = struct.pack("<%dH" % len(pixels), *pixels)

    if compressed:
        body = bytearray()
        i = 0
        while i < len(raw):
            n = min(0x4000, (len(raw) - i) // 2)
            body += struct.pack("<h", -n)
            body += raw[i : i + n * 2]
            i += n * 2
        out += struct.pack("<L", len(body)) + body
    else:
        out += raw
    return bytes(out)


def make_save(dirname="BASLUS-20678SAVE", files=None):
    """Build a :class:`ps2save.ps2_save_file` in memory."""
    if files is None:
        files = [
            ("icon.sys", make_icon_sys()),
            ("list.icn", make_icn()),
            ("game.dat", bytes(range(256)) * 40),
        ]
    sf = ps2save.ps2_save_file()
    now = tod_now()
    sf.set_directory(
        (DF_RWX | DF_DIR | DF_0400 | DF_EXISTS, 0, len(files), now,
         0, 0, now, 0, dirname)
    )
    for i, (name, data) in enumerate(files):
        sf.set_file(
            i,
            [DF_RWX | DF_FILE | DF_0400 | DF_EXISTS, 0, len(data), now,
             0, 0, now, 0, name],
            data,
        )
    return sf, files


@pytest.fixture
def blank_card():
    """A freshly formatted 8 MB card image, as an in-memory file."""
    buf = io.BytesIO()
    buf.name = "test.ps2"
    mc = ps2mc.ps2mc(buf, params=STANDARD_PARAMS)
    yield mc
    mc.close()


@pytest.fixture
def quiet():
    """A progress callback that reports nothing."""
    return lambda percent: None
