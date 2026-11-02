"""Quiet, compact native rendering for the task inbox and its navigation rail.

The delegate reads presentation metadata only. Check state remains in Qt's
standard CheckStateRole, so item selection and checkbox interaction continue to
use QStyledItemDelegate's normal editorEvent implementation.
"""

from __future__ import annotations

import math
from typing import Any

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QGuiApplication, QIcon, QPainter, QPainterPath, QPen, QPixmap, QPolygonF
from PySide6.QtWidgets import QApplication, QStyle, QStyledItemDelegate, QStyleOptionFocusRect, QStyleOptionViewItem

from .native_helpers import DECISIONS

__all__ = ["line_icon", "NativeListDelegate"]

_METADATA_ROLE = int(Qt.ItemDataRole.UserRole) + 1
_TEXT = QColor("#EEEFF3")
_MUTED = QColor("#8C8E98")
_SOFT = QColor("#AEB0BA")
_ACCENT = QColor("#9BAFF5")
_DECISION_COLORS = {
    "keep": "#9DC7B6", "exclude": "#C99BA5", "needs_info": "#CFBB90",
    "watch": "#ADBCE5", "unreviewed": "#8C8E98",
}


def _path(points: list[tuple[float, float]], close: bool = False) -> QPainterPath:
    path = QPainterPath(QPointF(*points[0]))
    for point in points[1:]:
        path.lineTo(QPointF(*point))
    if close:
        path.closeSubpath()
    return path


def _draw_icon(painter: QPainter, name: str, color: QColor) -> None:
    painter.setPen(QPen(color, 1.65, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    if name == "plus":
        painter.drawLine(QPointF(12, 5), QPointF(12, 19))
        painter.drawLine(QPointF(5, 12), QPointF(19, 12))
    elif name == "inbox":
        painter.drawPath(_path([(5, 4.5), (19, 4.5), (21, 13), (21, 19), (3, 19), (3, 13)], True))
        painter.drawPath(_path([(3, 13), (8, 13), (10, 16), (14, 16), (16, 13), (21, 13)]))
    elif name == "layers":
        painter.drawPath(_path([(3, 8), (12, 3), (21, 8), (12, 13)], True))
        painter.drawPath(_path([(3, 12), (12, 17), (21, 12)]))
        painter.drawPath(_path([(3, 16), (12, 21), (21, 16)]))
    elif name == "settings":
        points = []
        for tooth in range(8):
            for offset, radius in [(0, 7.2), (0.20, 9), (0.60, 9), (0.80, 7.2)]:
                angle = (tooth + offset) * math.tau / 8 - math.pi / 2
                points.append(QPointF(12 + radius * math.cos(angle), 12 + radius * math.sin(angle)))
        painter.drawPolygon(QPolygonF(points))
        painter.drawEllipse(QRectF(8.8, 8.8, 6.4, 6.4))
    elif name == "help":
        painter.drawEllipse(QRectF(3.5, 3.5, 17, 17))
        question = QPainterPath(QPointF(9, 9))
        question.cubicTo(9.2, 5.7, 15.4, 5.9, 15, 9.4)
        question.cubicTo(14.8, 11.4, 12, 11.3, 12, 14)
        painter.drawPath(question)
        painter.drawPoint(QPointF(12, 17))
    elif name == "search":
        painter.drawEllipse(QRectF(3.7, 3.7, 12.6, 12.6))
        painter.drawLine(QPointF(14.7, 14.7), QPointF(20.3, 20.3))
    elif name == "chevron":
        painter.drawPath(_path([(9, 5.5), (15.5, 12), (9, 18.5)]))
    elif name == "external":
        painter.drawPath(_path([(14, 3.5), (20.5, 3.5), (20.5, 10)]))
        painter.drawLine(QPointF(20.5, 3.5), QPointF(10, 14))
        painter.drawPath(_path([(10, 4.5), (4.5, 4.5), (4.5, 19.5), (19.5, 19.5), (19.5, 14)]))
    elif name == "refresh":
        arc = QPainterPath()
        arc.arcMoveTo(QRectF(4.5, 4.5, 15, 15), 45)
        arc.arcTo(QRectF(4.5, 4.5, 15, 15), 45, 285)
        painter.drawPath(arc)
        painter.drawPath(_path([(15, 4), (20, 4), (20, 9)]))
    elif name == "check":
        painter.drawPath(_path([(4.5, 12), (9.5, 17), (19.5, 6)]))
    elif name == "more":
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        for x in (5, 12, 19):
            painter.drawEllipse(QPointF(x, 12), 1.35, 1.35)
    else:
        painter.drawEllipse(QRectF(4, 4, 16, 16))


def line_icon(name: str, color: str = "#8C8E98", size: int = 20) -> QIcon:
    """Make a crisp, antialiased line icon at 1x, 2x and 3x device density."""
    if QGuiApplication.instance() is None:
        raise RuntimeError("请先创建 Qt 应用，再创建界面图标。")
    size = max(8, min(256, int(size)))
    ink = QColor(color)
    if not ink.isValid():
        ink = QColor("#8C8E98")
    icon = QIcon()
    states = [
        (QIcon.Mode.Normal, QIcon.State.Off, ink),
        (QIcon.Mode.Normal, QIcon.State.On, QColor("#C3CFF9")),
        (QIcon.Mode.Active, QIcon.State.Off, QColor("#BFC8DF")),
        (QIcon.Mode.Active, QIcon.State.On, QColor("#C3CFF9")),
        (QIcon.Mode.Selected, QIcon.State.Off, QColor("#C3CFF9")),
        (QIcon.Mode.Disabled, QIcon.State.Off, QColor("#525661")),
        (QIcon.Mode.Disabled, QIcon.State.On, QColor("#68728E")),
    ]
    for mode, state, state_ink in states:
        for density in (1, 2, 3):
            pixmap = QPixmap(size * density, size * density)
            pixmap.setDevicePixelRatio(density)
            pixmap.fill(Qt.GlobalColor.transparent)
            painter = QPainter(pixmap)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.scale(size / 24, size / 24)
            _draw_icon(painter, str(name).lower(), state_ink)
            painter.end()
            icon.addPixmap(pixmap, mode, state)
    return icon


def _single_line(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(str(value).split())


def _font(base: QFont, pixels: int) -> QFont:
    font = QFont(base)
    font.setPixelSize(pixels)
    font.setWeight(QFont.Weight.Normal)
    font.setItalic(False)
    return font


class NativeListDelegate(QStyledItemDelegate):
    """Render flat task/candidate rows from UserRole+1 metadata dictionaries."""

    METADATA_ROLE = _METADATA_ROLE

    @staticmethod
    def _metadata(index) -> dict:
        value = index.data(_METADATA_ROLE)
        return value if isinstance(value, dict) else {}

    def sizeHint(self, option, index) -> QSize:
        return QSize(220, 112 if self._metadata(index).get("kind") == "candidate" else 64)

    @staticmethod
    def _text(painter: QPainter, rect: QRectF, text: Any, font: QFont, color: QColor) -> None:
        if rect.width() <= 0:
            return
        painter.setFont(font)
        painter.setPen(color)
        elided = QFontMetrics(font).elidedText(_single_line(text), Qt.TextElideMode.ElideRight, int(rect.width()))
        painter.drawText(rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, elided)

    @staticmethod
    def _badge(metadata: dict) -> str:
        badge = metadata.get("badge")
        if isinstance(badge, dict):
            badge = badge.get("label") or badge.get("text")
        badge = _single_line(badge)
        if not badge:
            badge = DECISIONS.get(metadata.get("decision"), "")
        if metadata.get("demo"):
            badge = f"演示 · {badge}" if badge and badge != "演示" else "演示"
        return badge

    def paint(self, painter: QPainter, option, index) -> None:
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        metadata = self._metadata(index)
        candidate = metadata.get("kind") == "candidate"
        selected = bool(opt.state & QStyle.StateFlag.State_Selected)
        hovered = bool(opt.state & QStyle.StateFlag.State_MouseOver)
        enabled = bool(opt.state & QStyle.StateFlag.State_Enabled)
        widget = opt.widget
        style = widget.style() if widget is not None else QApplication.style()
        rect = QRectF(opt.rect)
        painter.save()
        painter.setClipRect(opt.rect)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if selected:
            painter.fillRect(rect, QColor("#202431"))
            painter.fillRect(QRectF(rect.left(), rect.top() + 9, 2, rect.height() - 18), _ACCENT)
        elif hovered:
            painter.fillRect(rect, QColor("#1B1D24"))

        left = rect.left() + 16
        if opt.features & QStyleOptionViewItem.ViewItemFeature.HasCheckIndicator:
            # This is deliberately the very same rectangle used by Qt's normal
            # editorEvent hit test. Never move the painted box independently.
            check_rect = style.subElementRect(QStyle.SubElement.SE_ItemViewItemCheckIndicator, opt, widget)
            indicator = QStyleOptionViewItem(opt)
            indicator.rect = check_rect
            indicator.state &= ~(QStyle.StateFlag.State_HasFocus | QStyle.StateFlag.State_On | QStyle.StateFlag.State_Off | QStyle.StateFlag.State_NoChange)
            if opt.checkState == Qt.CheckState.Checked:
                indicator.state |= QStyle.StateFlag.State_On
            elif opt.checkState == Qt.CheckState.PartiallyChecked:
                indicator.state |= QStyle.StateFlag.State_NoChange
            else:
                indicator.state |= QStyle.StateFlag.State_Off
            style.drawPrimitive(QStyle.PrimitiveElement.PE_IndicatorItemViewItemCheck, indicator, painter, widget)
            left = max(left, check_rect.right() + 12)

        right = rect.right() - 16
        width = max(0, right - left)
        title_font = _font(opt.font, 15)
        body_font = _font(opt.font, 13)
        meta_font = _font(opt.font, 12)
        title_color = _TEXT if enabled else QColor("#686B76")
        title = metadata.get("title") or opt.text
        subtitle = metadata.get("subtitle") or ""
        badge = self._badge(metadata)
        badge_color = QColor(_DECISION_COLORS.get(metadata.get("decision"), "#8C8E98"))

        if candidate:
            self._text(painter, QRectF(left, rect.top() + 11, width, 23), title, title_font, title_color)
            self._text(painter, QRectF(left, rect.top() + 36, width, 18), subtitle, meta_font, _MUTED)
            self._text(painter, QRectF(left, rect.top() + 58, width, 20), metadata.get("excerpt") or "", body_font, _SOFT)
            bottom = rect.top() + 85
            trail = " · ".join(filter(None, [_single_line(metadata.get("platform")), _single_line(metadata.get("time"))]))
        else:
            self._text(painter, QRectF(left, rect.top() + 9, width, 23), title, title_font, title_color)
            bottom = rect.top() + 35
            trail = _single_line(subtitle) or " · ".join(filter(None, [_single_line(metadata.get("platform")), _single_line(metadata.get("time"))]))

        badge_width = min(width * .48, QFontMetrics(meta_font).horizontalAdvance(badge) + 1) if badge else 0
        meta_width = max(0, width - badge_width - (12 if badge else 0))
        self._text(painter, QRectF(left, bottom, meta_width, 18), trail, meta_font, _MUTED)
        if badge:
            self._text(painter, QRectF(right - badge_width, bottom, badge_width, 18), badge, meta_font, badge_color)
        # A single quiet divider replaces individual cards and outlines.
        painter.setPen(QPen(QColor("#282A33"), 1))
        painter.drawLine(QPointF(left, rect.bottom() - .5), QPointF(right, rect.bottom() - .5))
        if opt.state & QStyle.StateFlag.State_HasFocus:
            focus = QStyleOptionFocusRect()
            if widget is not None:
                focus.initFrom(widget)
            focus.rect = opt.rect.adjusted(4, 3, -4, -3)
            focus.state = opt.state
            focus.backgroundColor = QColor("#202431" if selected else "#101114")
            style.drawPrimitive(QStyle.PrimitiveElement.PE_FrameFocusRect, focus, painter, widget)
        painter.restore()
