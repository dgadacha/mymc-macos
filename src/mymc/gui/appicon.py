"""The application icon, drawn rather than shipped.

The original carried 2004-era Windows ``.ico`` blobs base64 encoded in a
Python source file; at 32x32 they look rough on a Retina display.  These
are drawn with QPainter instead, so they stay sharp at any size.
"""

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QIcon,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPixmap,
)

_CARD_BODY = QColor("#3b4252")
_CARD_EDGE = QColor("#1f232b")
_CARD_TOP = QColor("#5a6683")
_LABEL = QColor("#e8ecf5")
_ACCENT = QColor("#4c8dff")


def _draw_card(painter: QPainter, size: int):
    """Draw a PS2 memory card silhouette filling a size x size box."""
    s = size
    painter.setRenderHint(QPainter.Antialiasing, True)

    body = QRectF(s * 0.17, s * 0.09, s * 0.66, s * 0.82)
    radius = s * 0.09

    path = QPainterPath()
    path.addRoundedRect(body, radius, radius)

    gradient = QLinearGradient(body.topLeft(), body.bottomRight())
    gradient.setColorAt(0.0, _CARD_TOP)
    gradient.setColorAt(1.0, _CARD_BODY)
    painter.fillPath(path, QBrush(gradient))

    pen_width = max(1.0, s * 0.022)
    painter.setPen(QColor(_CARD_EDGE))
    painter.save()
    p = painter.pen()
    p.setWidthF(pen_width)
    painter.setPen(p)
    painter.drawPath(path)
    painter.restore()

    # the connector slot along the top edge
    connector = QRectF(s * 0.28, s * 0.14, s * 0.44, s * 0.11)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor(_CARD_EDGE))
    painter.drawRoundedRect(connector, s * 0.02, s * 0.02)
    painter.setBrush(_ACCENT)
    for i in range(4):
        x = connector.left() + s * 0.035 + i * s * 0.105
        painter.drawRect(QRectF(x, connector.top() + s * 0.025, s * 0.05, s * 0.06))

    # the white label area
    label = QRectF(s * 0.25, s * 0.36, s * 0.50, s * 0.42)
    painter.setBrush(_LABEL)
    painter.drawRoundedRect(label, s * 0.035, s * 0.035)

    painter.setBrush(QColor("#98a2b8"))
    for i in range(3):
        y = label.top() + s * 0.09 + i * s * 0.11
        width = label.width() * (0.72 if i != 1 else 0.52)
        painter.drawRoundedRect(
            QRectF(label.left() + s * 0.06, y, width, s * 0.045), s * 0.02, s * 0.02
        )


def app_icon() -> QIcon:
    """Build the multi-resolution application icon."""
    icon = QIcon()
    for size in (16, 32, 64, 128, 256, 512, 1024):
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        _draw_card(painter, size)
        painter.end()
        icon.addPixmap(pixmap)
    return icon


def _glyph(size: int, draw) -> QPixmap:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing, True)
    draw(painter, size)
    painter.end()
    return pixmap


def _arrow(painter, size, up, color):
    s = size
    pen = painter.pen()
    pen.setColor(color)
    pen.setWidthF(max(1.6, s * 0.11))
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    painter.setPen(pen)

    cx = s * 0.5
    top, bottom = s * 0.20, s * 0.66
    painter.drawLine(cx, top if up else bottom, cx, bottom if up else top)
    head = s * 0.17
    tip_y = top if up else bottom
    dy = head if up else -head
    painter.drawLine(cx - head, tip_y + dy, cx, tip_y)
    painter.drawLine(cx + head, tip_y + dy, cx, tip_y)
    painter.drawLine(s * 0.16, s * 0.84, s * 0.84, s * 0.84)


def toolbar_icons(color: QColor) -> dict:
    """Simple monochrome toolbar glyphs that follow the current theme."""

    def open_card(painter, s):
        _draw_card(painter, s)

    def import_glyph(painter, s):
        _arrow(painter, s, False, color)

    def export_glyph(painter, s):
        _arrow(painter, s, True, color)

    def delete_glyph(painter, s):
        pen = painter.pen()
        pen.setColor(color)
        pen.setWidthF(max(1.6, s * 0.10))
        pen.setCapStyle(Qt.RoundCap)
        painter.setPen(pen)
        painter.drawLine(s * 0.20, s * 0.28, s * 0.80, s * 0.28)
        painter.drawLine(s * 0.40, s * 0.28, s * 0.42, s * 0.18)
        painter.drawLine(s * 0.60, s * 0.28, s * 0.58, s * 0.18)
        painter.drawLine(s * 0.42, s * 0.18, s * 0.58, s * 0.18)
        painter.drawLine(s * 0.27, s * 0.28, s * 0.32, s * 0.82)
        painter.drawLine(s * 0.73, s * 0.28, s * 0.68, s * 0.82)
        painter.drawLine(s * 0.32, s * 0.82, s * 0.68, s * 0.82)

    return {
        name: QIcon(_glyph(64, fn))
        for name, fn in (
            ("open", open_card),
            ("import", import_glyph),
            ("export", export_glyph),
            ("delete", delete_glyph),
        )
    }
