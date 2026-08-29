"""Parser for the PlayStation 2 save file 3D icon format (``.icn``).

The original mymc could only display these icons on Windows, through a
Direct3D helper DLL.  This module decodes the format into plain NumPy
arrays so that :mod:`mymc.gui.render` can draw them anywhere.

Layout of an ``.icn`` file::

    u32 file_id             always 0x00010000
    u32 animation_shapes    number of morph targets
    u32 texture_type        bit 0-2: texture present, bit 3: RLE compressed
    u32 reserved            always 0x3F800000
    u32 vertex_count

    vertex_count times:
        animation_shapes x  s16 x, y, z, pad     (position, 1/4096 units)
        s16 nx, ny, nz, pad                      (normal, 1/4096 units)
        s16 u, v                                 (texture coords, 1/4096)
        u8  r, g, b, a                           (vertex colour)

    u32 id_tag, u32 frame_length, f32 anim_speed,
    u32 play_offset, u32 frame_count
    frame_count times:
        u32 shape_id, u32 key_count
        key_count x (f32 time, f32 value)

    texture: 128x128 pixels, 16 bit RGBA5551, optionally RLE compressed

Vertices are grouped in threes: every three consecutive vertices form one
triangle.
"""

import struct

import numpy as np

__all__ = ["Icon", "IconError", "parse_icon", "TEXTURE_SIZE"]

TEXTURE_SIZE = 128

_FIXED = 4096.0

_header_struct = struct.Struct("<5L")
_anim_header_struct = struct.Struct("<LLfLL")


class IconError(Exception):
    """Raised when an .icn file cannot be understood."""


def _decode_rle_texture(data: bytes) -> np.ndarray:
    """Decompress the run-length encoded variant of the texture."""
    (size,) = struct.unpack_from("<L", data, 0)
    src = np.frombuffer(data, dtype="<u2", count=size // 2, offset=4)
    out = np.empty(TEXTURE_SIZE * TEXTURE_SIZE, dtype="<u2")
    pos = 0
    i = 0
    n = len(src)
    while pos < out.size and i < n:
        code = int(src[i])
        i += 1
        if code & 0x8000:
            # a run of literal pixels
            count = 0x10000 - code
            count = min(count, out.size - pos, n - i)
            out[pos : pos + count] = src[i : i + count]
            i += count
            pos += count
        else:
            if i >= n:
                break
            count = min(code, out.size - pos)
            out[pos : pos + count] = src[i]
            i += 1
            pos += count
    if pos < out.size:
        out[pos:] = 0
    return out


def _rgba5551_to_rgb(pixels: np.ndarray) -> np.ndarray:
    """Expand 16 bit RGBA5551 pixels to 8 bit per channel RGB."""
    px = pixels.astype(np.uint16)
    r = (px & 0x1F).astype(np.uint8)
    g = ((px >> 5) & 0x1F).astype(np.uint8)
    b = ((px >> 10) & 0x1F).astype(np.uint8)
    # 5 bit -> 8 bit, keeping full range (0 -> 0, 31 -> 255)
    expand = lambda v: (v << 3) | (v >> 2)
    return np.stack([expand(r), expand(g), expand(b)], axis=-1)


class Icon(object):
    """A decoded PS2 save file icon."""

    def __init__(
        self,
        shapes,
        normals,
        uvs,
        colors,
        texture,
        frame_length,
        anim_speed,
        frames,
    ):
        #: (shape_count, vertex_count, 3) float32 vertex positions
        self.shapes = shapes
        #: (vertex_count, 3) float32 vertex normals
        self.normals = normals
        #: (vertex_count, 2) float32 texture coordinates
        self.uvs = uvs
        #: (vertex_count, 4) float32 vertex colours, 0..1
        self.colors = colors
        #: (128, 128, 3) uint8 texture, or None
        self.texture = texture
        self.frame_length = frame_length
        self.anim_speed = anim_speed
        self.frames = frames

    @property
    def shape_count(self):
        return self.shapes.shape[0]

    @property
    def vertex_count(self):
        return self.shapes.shape[1]

    @property
    def triangle_count(self):
        return self.vertex_count // 3

    def shape_at(self, position: float) -> np.ndarray:
        """Blend between morph targets.

        ``position`` walks through the shapes and wraps around, so an
        icon with several shapes gently morphs from one to the next.
        """
        n = self.shape_count
        if n == 1:
            return self.shapes[0]
        position = position % n
        i = int(position)
        t = position - i
        if t == 0.0:
            return self.shapes[i]
        a = self.shapes[i]
        b = self.shapes[(i + 1) % n]
        return a * (1.0 - t) + b * t


def parse_icon(data: bytes) -> Icon:
    """Parse the contents of an ``.icn`` file."""
    if len(data) < _header_struct.size:
        raise IconError("icon file is too short")

    (file_id, shape_count, texture_type, reserved, vertex_count) = (
        _header_struct.unpack_from(data, 0)
    )

    if shape_count < 1 or shape_count > 64:
        raise IconError("implausible animation shape count (%d)" % shape_count)
    if vertex_count < 3 or vertex_count > 1 << 20:
        raise IconError("implausible vertex count (%d)" % vertex_count)

    vertex_dtype = np.dtype(
        [
            ("shapes", "<i2", (shape_count, 4)),
            ("normal", "<i2", 4),
            ("uv", "<i2", 2),
            ("color", "u1", 4),
        ]
    )
    offset = _header_struct.size
    needed = vertex_dtype.itemsize * vertex_count
    if len(data) < offset + needed:
        raise IconError("icon file is truncated (vertex data)")

    verts = np.frombuffer(data, dtype=vertex_dtype, count=vertex_count, offset=offset)
    offset += needed

    shapes = (
        verts["shapes"][:, :, :3].astype(np.float32).transpose(1, 0, 2) / _FIXED
    )
    normals = verts["normal"][:, :3].astype(np.float32) / _FIXED
    uvs = verts["uv"].astype(np.float32) / _FIXED
    colors = verts["color"].astype(np.float32) / 255.0

    # Normalise the normals; a few icons ship denormalised ones.
    lengths = np.linalg.norm(normals, axis=1, keepdims=True)
    normals = np.divide(normals, lengths, out=np.zeros_like(normals), where=lengths > 0)

    frame_length = 1
    anim_speed = 0.0
    frames = []
    if len(data) >= offset + _anim_header_struct.size:
        (id_tag, frame_length, anim_speed, play_offset, frame_count) = (
            _anim_header_struct.unpack_from(data, offset)
        )
        offset += _anim_header_struct.size
        if frame_count > 4096:
            frame_count = 0
        for _ in range(frame_count):
            if len(data) < offset + 8:
                break
            (shape_id, key_count) = struct.unpack_from("<LL", data, offset)
            offset += 8
            if key_count > 4096:
                break
            keys = []
            for _ in range(key_count):
                if len(data) < offset + 8:
                    break
                keys.append(struct.unpack_from("<ff", data, offset))
                offset += 8
            frames.append((shape_id, keys))
    frame_length = max(1, min(int(frame_length), 4096))

    texture = None
    rest = data[offset:]
    if texture_type & 0x07 and rest:
        try:
            if texture_type & 0x08:
                pixels = _decode_rle_texture(rest)
            else:
                want = TEXTURE_SIZE * TEXTURE_SIZE
                pixels = np.frombuffer(rest, dtype="<u2", count=want)
            texture = _rgba5551_to_rgb(pixels).reshape(
                TEXTURE_SIZE, TEXTURE_SIZE, 3
            )
        except (ValueError, struct.error):
            texture = None

    return Icon(
        np.ascontiguousarray(shapes),
        normals,
        uvs,
        colors,
        texture,
        frame_length,
        anim_speed,
        frames,
    )
