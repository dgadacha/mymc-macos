"""Tests for the .icn parser and the software icon renderer."""

import pytest

from conftest import make_icn

np = pytest.importorskip("numpy")

from mymc import ps2icon, render  # noqa: E402  (needs NumPy)


class TestParser:
    def test_parses_a_cube(self):
        icon = ps2icon.parse_icon(make_icn(shapes=2))
        assert icon.shape_count == 2
        assert icon.vertex_count == 36
        assert icon.triangle_count == 12
        assert icon.texture.shape == (128, 128, 3)
        assert icon.frame_length == 60

    def test_rle_texture_matches_uncompressed(self):
        plain = ps2icon.parse_icon(make_icn(compressed=False))
        packed = ps2icon.parse_icon(make_icn(compressed=True))
        assert np.array_equal(plain.texture, packed.texture)
        assert np.array_equal(plain.shapes, packed.shapes)

    def test_normals_are_unit_length(self):
        icon = ps2icon.parse_icon(make_icn())
        lengths = np.linalg.norm(icon.normals, axis=1)
        assert np.allclose(lengths, 1.0, atol=1e-3)

    def test_shape_blending(self):
        icon = ps2icon.parse_icon(make_icn(shapes=2))
        assert np.array_equal(icon.shape_at(0.0), icon.shapes[0])
        assert np.array_equal(icon.shape_at(1.0), icon.shapes[1])
        halfway = icon.shape_at(0.5)
        assert np.allclose(halfway, (icon.shapes[0] + icon.shapes[1]) / 2)

    def test_single_shape_icon_does_not_blend(self):
        icon = ps2icon.parse_icon(make_icn(shapes=1))
        assert np.array_equal(icon.shape_at(0.7), icon.shapes[0])

    @pytest.mark.parametrize("data", [b"", b"\0" * 8, b"\xff" * 64])
    def test_rejects_junk(self, data):
        with pytest.raises(ps2icon.IconError):
            ps2icon.parse_icon(data)

    def test_rejects_truncated_vertex_data(self):
        good = make_icn()
        with pytest.raises(ps2icon.IconError):
            ps2icon.parse_icon(good[:40])


class TestRenderer:
    @pytest.fixture
    def icon(self):
        return ps2icon.parse_icon(make_icn())

    def test_renders_something(self, icon):
        r = render.IconRenderer(96, 96)
        img = r.render(icon, background=(0.0, 0.0, 0.0))
        assert img.shape == (96, 96, 3)
        assert img.dtype == np.uint8
        lit = (img.reshape(-1, 3).max(axis=1) > 40).sum()
        assert lit > 96 * 96 * 0.15  # the model actually covers the frame

    def test_background_is_respected(self, icon):
        r = render.IconRenderer(64, 64)
        img = r.render(icon, background=(1.0, 0.0, 0.0))
        corner = img[0, 0]
        assert corner[0] > 200 and corner[1] < 50 and corner[2] < 50

    def test_auto_fit_is_scale_invariant(self, icon):
        r = render.IconRenderer(96, 96)
        small = r.render(icon, angle=0.6)
        bigger = ps2icon.Icon(
            icon.shapes * 9.0, icon.normals, icon.uvs, icon.colors,
            icon.texture, icon.frame_length, icon.anim_speed, icon.frames,
        )
        assert np.array_equal(small, r.render(bigger, angle=0.6))

    def _one_triangle(self, corner):
        """An icon holding a single triangle around a given point."""
        centre = np.asarray(corner, dtype=np.float32)
        offsets = np.array(
            [[-0.2, -0.2, 0.0], [0.2, -0.2, 0.0], [0.0, 0.2, 0.0]], np.float32
        )
        verts = (centre + offsets).astype(np.float32)
        normals = np.tile(np.array([0, 0, -1], np.float32), (3, 1))
        return ps2icon.Icon(
            verts[None, ...], normals, np.zeros((3, 2), np.float32),
            np.ones((3, 4), np.float32), None, 1, 0.0, [],
        )

    def _centre_of_mass(self, img):
        lit = img.reshape(img.shape[0], img.shape[1], 3).max(axis=2) > 40
        assert lit.any(), "nothing was drawn"
        ys, xs = np.nonzero(lit)
        return xs.mean() / img.shape[1], ys.mean() / img.shape[0]

    def test_model_is_not_mirrored(self):
        # A triangle sitting on the model's +X side has to appear on the
        # right of the frame.  Getting the view basis handedness wrong
        # mirrors every icon, which reads as backwards text on real saves.
        r = render.IconRenderer(128, 128)
        img = r.render(self._one_triangle((1.0, 0.0, 0.0)),
                       camera=(0.0, 0.0, -8.0), fit=False,
                       lighting=render.LIGHTING_PRESETS["none"])
        x, _ = self._centre_of_mass(img)
        assert x > 0.55, "model is mirrored horizontally (x=%.2f)" % x

    def test_model_is_not_upside_down(self):
        # PS2 icon space has Y pointing down, so a triangle at +Y in file
        # coordinates must come out at the bottom of the frame.
        r = render.IconRenderer(128, 128)
        img = r.render(self._one_triangle((0.0, 1.0, 0.0)),
                       camera=(0.0, 0.0, -8.0), fit=False,
                       lighting=render.LIGHTING_PRESETS["none"])
        _, y = self._centre_of_mass(img)
        assert y > 0.55, "model is flipped vertically (y=%.2f)" % y

    def test_rotation_changes_the_image(self, icon):
        r = render.IconRenderer(96, 96)
        assert not np.array_equal(r.render(icon, angle=0.0),
                                  r.render(icon, angle=1.0))

    def test_untextured_differs_from_textured(self, icon):
        r = render.IconRenderer(96, 96)
        assert not np.array_equal(r.render(icon, textured=True),
                                  r.render(icon, textured=False))

    def test_depth_buffer_hides_back_faces(self, icon):
        # With a cube the near face must win; rendering twice in a row must
        # give the identical result (the buffers are reset each time).
        r = render.IconRenderer(96, 96)
        assert np.array_equal(r.render(icon, angle=0.4), r.render(icon, angle=0.4))

    def test_lighting_presets_differ(self, icon):
        r = render.IconRenderer(96, 96)
        none = r.render(icon, lighting=render.LIGHTING_PRESETS["none"])
        alt = r.render(icon, lighting=render.LIGHTING_PRESETS["alternate"])
        assert not np.array_equal(none, alt)

    def test_lighting_from_icon_sys(self):
        from conftest import make_icon_sys
        from mymc import ps2save

        icon_sys = ps2save.unpack_icon_sys(make_icon_sys())
        lighting = render.lighting_from_icon_sys(icon_sys)
        assert lighting.directions.shape == (3, 3)
        assert lighting.colors.shape == (3, 3)

    def test_all_black_lighting_falls_back(self):
        icon_sys = [None] * 18
        for i in range(7, 14):
            icon_sys[i] = (0.0, 0.0, 0.0, 0.0)
        lighting = render.lighting_from_icon_sys(icon_sys)
        assert lighting is render.LIGHTING_PRESETS["alternate"]

    def test_png_output(self, icon, tmp_path):
        path = tmp_path / "icon.png"
        render.render_to_png(icon, str(path), size=64)
        data = path.read_bytes()
        assert data.startswith(b"\x89PNG\r\n\x1a\n")
        assert b"IHDR" in data and b"IEND" in data
