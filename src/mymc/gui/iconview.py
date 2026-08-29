"""A Qt widget that displays a save file's animated 3D icon."""

import math

import numpy as np
from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtGui import QAction, QActionGroup, QImage, QPainter, QPixmap
from PySide6.QtWidgets import QMenu, QSizePolicy, QWidget

from .. import ps2icon, render

#: How many rendered frames make up one loop of the animation.
CYCLE_FRAMES = 48
FRAME_MS = 40  # 25 fps, close to the console's own icon animation


class IconView(QWidget):
    """Renders a :class:`~mymc.ps2icon.Icon`, animated and draggable.

    Frames are cached as they are produced, so a looping icon costs
    nothing after its first turn.  Drag with the mouse to spin the model;
    right-click for lighting and camera options.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(160, 160)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setContextMenuPolicy(Qt.DefaultContextMenu)

        self._icon = None
        self._renderer = render.IconRenderer(160, 160)
        self._lighting = None
        self._lighting_name = "icon.sys"
        self._camera = "default"
        self._animate = True
        self._textured = True
        self._frame = 0
        self._angle = 0.0
        self._drag_origin = None
        self._drag_angle = 0.0
        self._cache = {}
        self._placeholder = "No memory card open"

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._advance)
        self._timer.setInterval(FRAME_MS)

    #
    # Public API
    #

    def set_icon(self, icon, lighting=None, placeholder="No icon"):
        """Show an icon, or ``None`` to show a placeholder message."""
        self._icon = icon
        self._lighting = lighting
        self._placeholder = placeholder
        self._frame = 0
        self.invalidate()
        if icon is not None and self._animate and self._is_animated():
            self._timer.start()
        else:
            self._timer.stop()
        self.update()

    def invalidate(self):
        self._cache.clear()

    def set_animate(self, on):
        self._animate = bool(on)
        if self._animate and self._icon is not None and self._is_animated():
            self._timer.start()
        else:
            self._timer.stop()
        self.update()

    def set_camera(self, name):
        self._camera = name
        self.invalidate()
        self.update()

    def set_lighting(self, name):
        self._lighting_name = name
        self.invalidate()
        self.update()

    def set_textured(self, on):
        self._textured = bool(on)
        self.invalidate()
        self.update()

    #
    # Internals
    #

    def _is_animated(self):
        return self._icon is not None and self._icon.shape_count > 1

    def _effective_lighting(self):
        if self._lighting_name == "icon.sys":
            return self._lighting or render.LIGHTING_PRESETS["alternate"]
        return render.LIGHTING_PRESETS.get(
            self._lighting_name, render.LIGHTING_PRESETS["alternate"]
        )

    def _advance(self):
        self._frame = (self._frame + 1) % CYCLE_FRAMES
        self.update()

    def _background(self):
        c = self.palette().window().color()
        return (c.redF(), c.greenF(), c.blueF())

    def _render_frame(self):
        key = (self._frame, round(self._angle, 3), self._camera,
               self._lighting_name, self._textured, self.width(), self.height())
        cached = self._cache.get(key)
        if cached is not None:
            return cached

        ratio = self.devicePixelRatioF()
        w = max(1, int(self.width() * ratio))
        h = max(1, int(self.height() * ratio))
        if (w, h) != (self._renderer.width, self._renderer.height):
            self._renderer.resize(w, h)

        shape_pos = 0.0
        if self._is_animated():
            # ping-pong through the morph targets so the loop is seamless
            n = self._icon.shape_count
            phase = self._frame / CYCLE_FRAMES * 2.0
            shape_pos = (phase if phase <= 1.0 else 2.0 - phase) * (n - 1)

        rgb = self._renderer.render(
            self._icon,
            shape_pos=shape_pos,
            angle=self._angle,
            camera=self._camera,
            lighting=self._effective_lighting(),
            background=self._background(),
            textured=self._textured,
        )
        rgb = np.ascontiguousarray(rgb)
        image = QImage(rgb.data, w, h, 3 * w, QImage.Format_RGB888).copy()
        image.setDevicePixelRatio(ratio)
        pixmap = QPixmap.fromImage(image)

        if len(self._cache) > CYCLE_FRAMES * 2:
            self._cache.clear()
        self._cache[key] = pixmap
        return pixmap

    #
    # Qt events
    #

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), self.palette().window())

        if self._icon is None:
            painter.setPen(self.palette().placeholderText().color())
            painter.drawText(self.rect(), Qt.AlignCenter | Qt.TextWordWrap,
                             self._placeholder)
            return

        painter.drawPixmap(0, 0, self._render_frame())

    def resizeEvent(self, event):
        self.invalidate()
        super().resizeEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self._icon is not None:
            self._drag_origin = event.position().toPoint()
            self._drag_angle = self._angle
            self.setCursor(Qt.ClosedHandCursor)

    def mouseMoveEvent(self, event):
        if self._drag_origin is None:
            return
        dx = event.position().toPoint().x() - self._drag_origin.x()
        # Negative so the model follows the pointer: dragging right turns
        # the near face right, bringing the model's left side into view.
        self._angle = self._drag_angle - dx * 0.015
        self.update()

    def mouseReleaseEvent(self, event):
        self._drag_origin = None
        self.unsetCursor()

    def mouseDoubleClickEvent(self, event):
        self._angle = 0.0
        self.update()

    def contextMenuEvent(self, event):
        if self._icon is None:
            return
        menu = QMenu(self)

        act = menu.addAction("Animate")
        act.setCheckable(True)
        act.setChecked(self._animate)
        act.setEnabled(self._is_animated())
        act.triggered.connect(self.set_animate)

        act = menu.addAction("Show texture")
        act.setCheckable(True)
        act.setChecked(self._textured)
        act.triggered.connect(self.set_textured)

        menu.addSeparator()
        light_menu = menu.addMenu("Lighting")
        group = QActionGroup(light_menu)
        for name, label in (
            ("icon.sys", "From the save file"),
            ("none", "None"),
            ("flat", "Flat"),
            ("alternate", "Alternate"),
            ("alternate2", "Alternate 2"),
        ):
            a = light_menu.addAction(label)
            a.setCheckable(True)
            a.setChecked(self._lighting_name == name)
            a.triggered.connect(lambda _=False, n=name: self.set_lighting(n))
            group.addAction(a)

        cam_menu = menu.addMenu("Camera")
        group2 = QActionGroup(cam_menu)
        for name in ("flat", "default", "near", "high"):
            a = cam_menu.addAction(name.capitalize())
            a.setCheckable(True)
            a.setChecked(self._camera == name)
            a.triggered.connect(lambda _=False, n=name: self.set_camera(n))
            group2.addAction(a)

        menu.addSeparator()
        menu.addAction("Reset rotation", self.mouseDoubleClickEvent)

        menu.exec(event.globalPos())
