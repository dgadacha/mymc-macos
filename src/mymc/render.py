"""A small software renderer for PlayStation 2 save file icons.

Drawing the 3D icons used to require the Windows-only ``mymcicon`` DLL and
Direct3D.  This module rasterises them with NumPy instead: no GPU, no
driver, no platform-specific code -- it just returns an RGB image you can
hand to Qt, save as a PNG, or ignore.

The pipeline is deliberately plain: transform, per-vertex lighting,
z-buffered triangle rasterisation with perspective-correct, nearest
neighbour texturing.  At the sizes an icon is displayed it comfortably
keeps up with the console's own animation rate.
"""

import numpy as np

from .ps2icon import Icon

__all__ = [
    "IconRenderer",
    "Lighting",
    "CAMERA_PRESETS",
    "LIGHTING_PRESETS",
    "lighting_from_icon_sys",
]

#: Camera positions carried over from the original Windows GUI.
CAMERA_PRESETS = {
    "default": (0.0, 4.0, -8.0),
    "high": (0.0, 7.0, -6.0),
    "near": (0.0, 3.0, -6.0),
    "flat": (0.0, 2.0, -7.5),
}


class Lighting(object):
    """Three directional lights plus an ambient term."""

    def __init__(self, directions, colors, ambient, use_vertex_color=True):
        self.directions = np.asarray(directions, dtype=np.float32).reshape(-1, 3)
        self.colors = np.asarray(colors, dtype=np.float32).reshape(-1, 3)
        self.ambient = np.asarray(ambient, dtype=np.float32).reshape(3)
        self.use_vertex_color = use_vertex_color

    def shade(self, normals: np.ndarray) -> np.ndarray:
        """Return an (n, 3) light intensity for each vertex normal."""
        out = np.tile(self.ambient, (normals.shape[0], 1))
        if self.directions.size:
            dirs = self.directions
            lengths = np.linalg.norm(dirs, axis=1, keepdims=True)
            dirs = np.divide(
                dirs, lengths, out=np.zeros_like(dirs), where=lengths > 0
            )
            intensity = np.clip(normals @ dirs.T, 0.0, None)  # (n, lights)
            out = out + intensity @ self.colors
        return out


#: Lighting presets carried over from the original Windows GUI.
LIGHTING_PRESETS = {
    "none": Lighting([], [], [1.0, 1.0, 1.0], use_vertex_color=False),
    "flat": Lighting([], [], [1.0, 1.0, 1.0], use_vertex_color=True),
    "alternate": Lighting(
        [[1, -1, 2], [-1, 1, -2], [0, 1, 0]],
        [[1, 1, 1], [1, 1, 1], [0.7, 0.7, 0.7]],
        [0.5, 0.5, 0.5],
    ),
    "alternate2": Lighting(
        [[1, -1, 2], [-1, 1, -2], [0, 4, 1]],
        [[0.7, 0.7, 0.7], [0.7, 0.7, 0.7], [0.2, 0.2, 0.2]],
        [0.3, 0.3, 0.3],
        use_vertex_color=False,
    ),
}


def lighting_from_icon_sys(icon_sys) -> Lighting:
    """Build a :class:`Lighting` from the values stored in an icon.sys.

    Falls back to the ``"alternate"`` preset when the file carries no
    usable light setup, which some saves do.
    """
    try:
        directions = [icon_sys[i][:3] for i in (7, 8, 9)]
        colors = [icon_sys[i][:3] for i in (10, 11, 12)]
        ambient = icon_sys[13][:3]
    except (TypeError, IndexError):
        return LIGHTING_PRESETS["alternate"]

    lighting = Lighting(directions, colors, ambient)
    # A save with everything zeroed would render pitch black.
    if float(lighting.colors.sum() + lighting.ambient.sum()) <= 0.01:
        return LIGHTING_PRESETS["alternate"]
    return lighting


class IconRenderer(object):
    """Renders :class:`~mymc.ps2icon.Icon` objects to RGB images."""

    def __init__(self, width=256, height=256, fov=40.0):
        self.resize(width, height)
        self.fov = fov

    def resize(self, width, height):
        self.width = max(1, int(width))
        self.height = max(1, int(height))
        self._color = np.zeros((self.height, self.width, 3), dtype=np.float32)
        self._depth = np.empty((self.height, self.width), dtype=np.float32)
        # Pixel centre coordinates, reused for every triangle.
        self._xs = np.arange(self.width, dtype=np.float32) + 0.5
        self._ys = np.arange(self.height, dtype=np.float32) + 0.5

    def render(
        self,
        icon: Icon,
        shape_pos=0.0,
        angle=0.0,
        camera="default",
        lighting=None,
        background=(0.0, 0.0, 0.0),
        textured=True,
        fit=True,
    ) -> np.ndarray:
        """Render one frame, returning an ``(h, w, 3)`` uint8 array.

        ``shape_pos`` blends between the icon's morph targets, ``angle``
        spins the model around its vertical axis (radians).  With ``fit``
        the camera is pulled back or pushed in so that the model fills the
        frame whatever its scale -- icons out in the wild vary more than
        the fixed camera positions of the original assumed.
        """
        if lighting is None:
            lighting = LIGHTING_PRESETS["alternate"]
        if isinstance(camera, str):
            camera = CAMERA_PRESETS.get(camera, CAMERA_PRESETS["default"])

        self._color[:] = np.asarray(background, dtype=np.float32)
        self._depth[:] = np.inf

        positions = icon.shape_at(shape_pos)
        normals = icon.normals

        # PS2 icon space has Y pointing down; flip it so the model stands
        # up, then spin it around that axis.
        flip = np.array(
            [[1, 0, 0], [0, -1, 0], [0, 0, 1]], dtype=np.float32
        )
        c, s = np.cos(angle), np.sin(angle)
        spin = np.array(
            [[c, 0, s], [0, 1, 0], [-s, 0, c]], dtype=np.float32
        )
        model = flip @ spin

        positions = positions @ model.T
        normals = normals @ model.T

        # Look from the camera position towards the origin.
        eye = np.asarray(camera, dtype=np.float32)
        if fit:
            # Centre the model on its bounding box before framing it, so
            # icons that sit off-origin do not float in a corner.
            lo = positions.min(axis=0)
            hi = positions.max(axis=0)
            positions = positions - (lo + hi) * 0.5
            radius = float(np.linalg.norm(positions, axis=1).max())
            if radius > 1e-4:
                # Distance at which a sphere of that radius just fits the
                # field of view, plus a little air around it.
                distance = radius / np.sin(np.radians(self.fov) * 0.5) * 1.12
                eye = eye * (distance / (np.linalg.norm(eye) or 1.0))
        forward = -eye
        forward = forward / (np.linalg.norm(forward) or 1.0)
        world_up = np.array([0.0, 1.0, 0.0], dtype=np.float32)
        # Right-handed basis: with X right, Y up and Z the viewing
        # direction, right is cross(up, forward) -- taking the cross
        # products the other way round mirrors the image.
        right = np.cross(world_up, forward)
        norm = np.linalg.norm(right)
        if norm < 1e-6:
            right = np.array([1.0, 0.0, 0.0], dtype=np.float32)
        else:
            right = right / norm
        up = np.cross(forward, right)
        view = np.stack([right, up, forward])  # rows

        eye_pos = (positions - eye) @ view.T  # (n, 3), z = depth into screen

        shade = lighting.shade(normals)
        if lighting.use_vertex_color:
            shade = shade * icon.colors[:, :3] * 2.0
        vertex_color = np.clip(shade, 0.0, 4.0)

        # Perspective projection.
        near = 0.05
        f = 1.0 / np.tan(np.radians(self.fov) * 0.5)
        aspect = self.width / self.height
        depth = eye_pos[:, 2]
        safe_depth = np.where(depth > near, depth, near)
        ndc_x = eye_pos[:, 0] * f / (aspect * safe_depth)
        ndc_y = eye_pos[:, 1] * f / safe_depth
        screen = np.empty((positions.shape[0], 3), dtype=np.float32)
        screen[:, 0] = (ndc_x + 1.0) * 0.5 * self.width
        screen[:, 1] = (1.0 - ndc_y) * 0.5 * self.height
        screen[:, 2] = depth

        texture = icon.texture if textured else None
        self._rasterize(icon, screen, vertex_color, texture, near)

        return (np.clip(self._color, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)

    def _rasterize(self, icon, screen, vertex_color, texture, near):
        color_buf = self._color
        depth_buf = self._depth
        width, height = self.width, self.height
        xs_all, ys_all = self._xs, self._ys

        ntri = icon.triangle_count
        tri = screen[: ntri * 3].reshape(ntri, 3, 3)
        col = vertex_color[: ntri * 3].reshape(ntri, 3, 3)
        tex = icon.uvs[: ntri * 3].reshape(ntri, 3, 2)

        tx = tri[:, :, 0]
        ty = tri[:, :, 1]
        depths = tri[:, :, 2]

        # Everything that can be decided for all triangles at once is:
        # visibility, bounding boxes, and the signed area.  Only the
        # per-pixel work is left in the Python loop.
        min_x = tx.min(axis=1)
        max_x = tx.max(axis=1)
        min_y = ty.min(axis=1)
        max_y = ty.max(axis=1)
        area = (tx[:, 1] - tx[:, 0]) * (ty[:, 2] - ty[:, 0]) - (
            tx[:, 2] - tx[:, 0]
        ) * (ty[:, 1] - ty[:, 0])

        visible = (depths > near).all(axis=1)
        visible &= (max_x > 0) & (min_x < width) & (max_y > 0) & (min_y < height)
        visible &= np.abs(area) > 1e-6
        todo = np.nonzero(visible)[0]
        if todo.size == 0:
            return

        with np.errstate(divide="ignore"):
            inv_area = np.where(area != 0, 1.0 / area, 0.0)
            inv_depth = 1.0 / depths

        # NumPy scalar indexing is slow; pull the per-triangle scalars into
        # Python lists once so the inner loop touches plain floats.
        l_x = tx.tolist()
        l_y = ty.tolist()
        l_iw = inv_depth.tolist()
        l_inv_area = inv_area.tolist()
        bx0 = np.clip(min_x.astype(np.int32), 0, width).tolist()
        bx1 = np.clip(max_x.astype(np.int32) + 2, 0, width).tolist()
        by0 = np.clip(min_y.astype(np.int32), 0, height).tolist()
        by1 = np.clip(max_y.astype(np.int32) + 2, 0, height).tolist()

        if texture is not None:
            tex_h, tex_w = texture.shape[:2]
            texf = texture.astype(np.float32) / 255.0
            l_u = (tex[:, :, 0] * tex_w).tolist()
            l_v = (tex[:, :, 1] * tex_h).tolist()
        else:
            texf = None

        for t in todo.tolist():
            x0, x1 = bx0[t], bx1[t]
            y0, y1 = by0[t], by1[t]
            if x1 <= x0 or y1 <= y0:
                continue

            (ax, bx, cx) = l_x[t]
            (ay, by, cy) = l_y[t]
            inv_a = l_inv_area[t]

            px = xs_all[x0:x1]
            py = ys_all[y0:y1][:, None]

            # Barycentric coordinates; dividing by the signed area makes
            # them positive inside the triangle for either winding.
            l0 = ((bx - px) * (cy - py) - (cx - px) * (by - py)) * inv_a
            l1 = ((cx - px) * (ay - py) - (ax - px) * (cy - py)) * inv_a
            l2 = 1.0 - l0 - l1
            inside = (l0 >= 0) & (l1 >= 0) & (l2 >= 0)
            if not inside.any():
                continue

            (iw0, iw1, iw2) = l_iw[t]
            w = l0 * iw0 + l1 * iw1 + l2 * iw2
            z = 1.0 / w  # perspective-correct view depth

            sub_depth = depth_buf[y0:y1, x0:x1]
            mask = inside & (z < sub_depth)
            if not mask.any():
                continue

            # Perspective-correct interpolation weights.
            n0 = (l0 * iw0 / w)[mask]
            n1 = (l1 * iw1 / w)[mask]
            n2 = (l2 * iw2 / w)[mask]

            c0, c1, c2 = col[t]
            shade = n0[:, None] * c0 + n1[:, None] * c1 + n2[:, None] * c2

            if texf is not None:
                (u0, u1, u2) = l_u[t]
                (v0, v1, v2) = l_v[t]
                iu = (n0 * u0 + n1 * u1 + n2 * u2).astype(np.int32)
                iv = (n0 * v0 + n1 * v1 + n2 * v2).astype(np.int32)
                np.clip(iu, 0, tex_w - 1, out=iu)
                np.clip(iv, 0, tex_h - 1, out=iv)
                shade *= texf[iv, iu]

            color_buf[y0:y1, x0:x1][mask] = shade
            sub_depth[mask] = z[mask]


def render_to_png(icon, path, size=256, **kwargs):
    """Convenience helper: render one frame and write it as a PNG."""
    img = IconRenderer(size, size).render(icon, **kwargs)
    _write_png(path, img)


def _write_png(path, rgb):
    """Write an RGB array as a PNG, without needing Pillow."""
    import struct
    import zlib

    height, width = rgb.shape[:2]
    raw = b"".join(
        b"\x00" + rgb[y].tobytes() for y in range(height)
    )

    def chunk(tag, data):
        c = tag + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c))

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(raw, 6))
    png += chunk(b"IEND", b"")
    with open(path, "wb") as f:
        f.write(png)
