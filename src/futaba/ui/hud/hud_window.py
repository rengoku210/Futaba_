"""
Futaba Floating HUD — 64x64 Minimalist Always-Available Overlay.

Features:
- Positioned in top-right corner of primary screen
- Frameless, translucent background, always on top
- Does NOT steal active window focus (Qt.Tool | Qt.WindowDoesNotAcceptFocus or custom flags)
- States: idle, listening, thinking, working, waiting, blocked, completed, error
- Animated waveform / pulse effect using QTimer
- Drag to reposition anywhere on screen
- Click to expand Quick Command Bar or open Dashboard
- Right-click context menu (Open Dashboard, Pause/Resume, DND, Exit)
"""

from __future__ import annotations

import math
import logging
from typing import Callable, Optional

from PySide6.QtCore import Qt, QTimer, QPoint, QRectF, QPointF, Signal
from PySide6.QtGui import (
    QPainter, QColor, QBrush, QPen, QRadialGradient, QLinearGradient,
    QMouseEvent, QContextMenuEvent, QCursor, QGuiApplication,
)
from PySide6.QtWidgets import QWidget, QMenu, QLineEdit, QVBoxLayout, QGraphicsDropShadowEffect

from futaba.ui.theme import (
    COLOR_ACCENT, COLOR_ACCENT_HOVER, COLOR_ACCENT_LIGHT,
    COLOR_SUCCESS, COLOR_WARNING, COLOR_BLOCKED, COLOR_ERROR,
    COLOR_BG_CARD, COLOR_BG_DARK, COLOR_TEXT_PRIMARY,
)

logger = logging.getLogger("futaba.ui.hud")


class FutabaHUD(QWidget):
    """
    The 64x64 floating Futaba HUD widget.
    """
    clicked = Signal()
    command_submitted = Signal(str)

    def __init__(
        self,
        size: int = 64,
        on_open_dashboard: Optional[Callable[[], None]] = None,
        on_pause_resume: Optional[Callable[[], None]] = None,
        on_toggle_dnd: Optional[Callable[[], None]] = None,
        on_toggle_gaming: Optional[Callable[[], None]] = None,
        on_talk_to_futaba: Optional[Callable[[], None]] = None,
        on_exit: Optional[Callable[[], None]] = None,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self._size = size
        self._state = "idle"
        self._status_text = "Futaba Idle"
        self._anim_phase = 0.0

        self.on_open_dashboard = on_open_dashboard
        self.on_pause_resume = on_pause_resume
        self.on_toggle_dnd = on_toggle_dnd
        self.on_toggle_gaming = on_toggle_gaming
        self.on_talk_to_futaba = on_talk_to_futaba
        self.on_exit = on_exit

        # Drag tracking
        self._drag_pos = QPoint()
        self._is_dragging = False

        # Quick command bar popup
        self._quick_input = None

        self._init_window()
        self._init_animation()

    def _init_window(self) -> None:
        """Set window flags for non-intrusive floating overlay."""
        self.setFixedSize(self._size, self._size)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)

        flags = (
            Qt.Window
            | Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.SubWindow
            | Qt.NoDropShadowWindowHint
        )
        self.setWindowFlags(flags)

        self.setToolTip("Futaba Copilot — Click to command, drag to move")
        self.setCursor(Qt.PointingHandCursor)

        # Position at top-right corner with 30px margin
        screen = QGuiApplication.primaryScreen()
        if screen:
            geom = screen.availableGeometry()
            x = geom.right() - self._size - 30
            y = geom.top() + 30
            self.move(x, y)

    def _init_animation(self) -> None:
        """Smooth 30 FPS animation timer."""
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._on_tick)
        self._timer.start(33)  # ~30 fps

    def _on_tick(self) -> None:
        self._anim_phase += 0.08
        if self._anim_phase > 2 * math.pi:
            self._anim_phase -= 2 * math.pi
        self.update()

    def set_state(self, state: str, status_text: str = "") -> None:
        """Update the HUD state and refresh appearance."""
        self._state = state.lower()
        if status_text:
            self._status_text = status_text
        self.setToolTip(f"Futaba [{self._state.upper()}]: {self._status_text}")
        self.update()

    # --- Painting ---

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        w = float(self.width())
        h = float(self.height())
        center = QPointF(w / 2.0, h / 2.0)
        radius = (min(w, h) / 2.0) - 4.0

        # State color map
        colors = {
            "idle": QColor(123, 47, 190),       # Futaba Purple
            "listening": QColor(59, 130, 246),   # Blue
            "speaking": QColor(52, 211, 153),   # Mint / Emerald
            "thinking": QColor(168, 85, 247),   # Lavender
            "tool_call": QColor(192, 132, 252), # Purple
            "executing": QColor(139, 92, 246),  # Indigo
            "working": QColor(139, 92, 246),    # Indigo
            "waiting": QColor(245, 158, 11),    # Amber
            "interrupted": QColor(251, 191, 36),# Amber
            "blocked": QColor(249, 115, 22),    # Orange
            "completed": QColor(16, 185, 129),  # Emerald
            "error": QColor(239, 68, 68),       # Crimson
        }
        accent = colors.get(self._state, colors["idle"])

        # 1. Breathing pulse amplitude
        pulse = 0.5 + 0.5 * math.sin(self._anim_phase)
        glow_radius = radius + (2.5 * pulse if self._state != "idle" else 1.0)

        # 2. Outer Glow
        glow = QRadialGradient(center, glow_radius)
        glow_alpha = int(140 + 70 * pulse) if self._state != "idle" else 90
        glow.setColorAt(0.0, QColor(accent.red(), accent.green(), accent.blue(), glow_alpha))
        glow.setColorAt(0.7, QColor(accent.red(), accent.green(), accent.blue(), 30))
        glow.setColorAt(1.0, QColor(accent.red(), accent.green(), accent.blue(), 0))
        painter.setBrush(QBrush(glow))
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(center, glow_radius, glow_radius)

        # 3. Base Dark Disc
        disc_radius = radius * 0.82
        base_grad = QRadialGradient(QPointF(center.x() - disc_radius * 0.2, center.y() - disc_radius * 0.2), disc_radius)
        base_grad.setColorAt(0.0, QColor(36, 32, 54, 240))
        base_grad.setColorAt(1.0, QColor(18, 17, 26, 250))
        painter.setBrush(QBrush(base_grad))
        painter.setPen(QPen(QColor(accent.red(), accent.green(), accent.blue(), 200), 1.5))
        painter.drawEllipse(center, disc_radius, disc_radius)

        # 4. State-specific visual representation
        if self._state in ("listening", "speaking"):
            # Audio waveform bars
            bar_color = QColor(167, 243, 208) if self._state == "speaking" else QColor(147, 197, 253)
            painter.setPen(QPen(bar_color, 2.0, Qt.SolidLine, Qt.RoundCap))
            num_bars = 5
            bar_spacing = 5.0
            start_x = center.x() - ((num_bars - 1) * bar_spacing) / 2.0
            for i in range(num_bars):
                bar_h = 4.0 + 8.0 * abs(math.sin(self._anim_phase * 1.5 + i * 0.8))
                bx = start_x + i * bar_spacing
                painter.drawLine(QPointF(bx, center.y() - bar_h), QPointF(bx, center.y() + bar_h))

        elif self._state in ("thinking", "working", "tool_call", "executing"):
            # Orbiting energy rings
            orbit_r = disc_radius * 0.55
            painter.setPen(QPen(QColor(accent.red(), accent.green(), accent.blue(), 100), 1.0))
            painter.setBrush(Qt.NoBrush)
            painter.drawEllipse(center, orbit_r, orbit_r)

            # Orbiting dot
            angle = self._anim_phase * 2.0
            dot_x = center.x() + orbit_r * math.cos(angle)
            dot_y = center.y() + orbit_r * math.sin(angle)
            painter.setBrush(QBrush(QColor(255, 255, 255, 240)))
            painter.setPen(Qt.NoPen)
            painter.drawEllipse(QPointF(dot_x, dot_y), 2.5, 2.5)

            # Inner stylized F
            self._draw_f_glyph(painter, center, disc_radius * 0.45)

        elif self._state == "blocked":
            # Exclamation mark
            painter.setPen(QPen(QColor(254, 215, 170), 2.5, Qt.SolidLine, Qt.RoundCap))
            painter.drawLine(QPointF(center.x(), center.y() - 7), QPointF(center.x(), center.y() + 1))
            painter.drawPoint(QPointF(center.x(), center.y() + 6))

        elif self._state == "completed":
            # Checkmark
            painter.setPen(QPen(QColor(167, 243, 208), 2.2, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            p1 = QPointF(center.x() - 6, center.y())
            p2 = QPointF(center.x() - 1, center.y() + 5)
            p3 = QPointF(center.x() + 7, center.y() - 5)
            painter.drawLine(p1, p2)
            painter.drawLine(p2, p3)

        elif self._state == "error":
            # Cross X
            painter.setPen(QPen(QColor(254, 202, 202), 2.2, Qt.SolidLine, Qt.RoundCap))
            s = 5.0
            painter.drawLine(QPointF(center.x() - s, center.y() - s), QPointF(center.x() + s, center.y() + s))
            painter.drawLine(QPointF(center.x() - s, center.y() + s), QPointF(center.x() + s, center.y() - s))

        else:
            # Idle / Default: Futaba "F" glyph
            self._draw_f_glyph(painter, center, disc_radius * 0.55)

        painter.end()

    def _draw_f_glyph(self, painter: QPainter, center: QPointF, size: float) -> None:
        """Draw the stylized Futaba F glyph."""
        painter.setPen(QPen(QColor(243, 244, 246, 230), 2.0, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        f_w = size * 0.55
        f_h = size * 0.8
        top_left = QPointF(center.x() - f_w * 0.45, center.y() - f_h * 0.5)

        # Stem
        painter.drawLine(top_left, QPointF(top_left.x(), top_left.y() + f_h))
        # Top bar
        painter.drawLine(top_left, QPointF(top_left.x() + f_w, top_left.y()))
        # Mid bar
        painter.drawLine(
            QPointF(top_left.x(), top_left.y() + f_h * 0.45),
            QPointF(top_left.x() + f_w * 0.75, top_left.y() + f_h * 0.45)
        )

    # --- Mouse & Interaction ---

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.LeftButton:
            self._is_dragging = True
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._is_dragging and event.buttons() & Qt.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_pos)
            event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.LeftButton:
            self._is_dragging = False
            # If not a long drag, treat as click
            self.clicked.emit()
            if self.on_open_dashboard:
                self.on_open_dashboard()
            event.accept()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.LeftButton:
            if self.on_talk_to_futaba:
                self.on_talk_to_futaba()
            event.accept()

    def contextMenuEvent(self, event: QContextMenuEvent) -> None:
        """Right-click menu."""
        menu = QMenu(self)
        menu.setStyleSheet(f"""
            QMenu {{
                background-color: {COLOR_BG_CARD};
                color: {COLOR_TEXT_PRIMARY};
                border: 1px solid {COLOR_ACCENT};
                border-radius: 6px;
                padding: 4px;
            }}
            QMenu::item {{
                padding: 6px 20px;
                border-radius: 4px;
            }}
            QMenu::item:selected {{
                background-color: {COLOR_ACCENT};
            }}
        """)

        act_talk = menu.addAction("🎤 Talk to Futaba")
        act_talk.triggered.connect(lambda: self.on_talk_to_futaba() if self.on_talk_to_futaba else None)

        act_dashboard = menu.addAction("Open Futaba Dashboard")
        act_dashboard.triggered.connect(lambda: self.on_open_dashboard() if self.on_open_dashboard else None)

        menu.addSeparator()

        act_pause = menu.addAction("Pause / Resume Tasks")
        act_pause.triggered.connect(lambda: self.on_pause_resume() if self.on_pause_resume else None)

        act_dnd = menu.addAction("Toggle Do Not Disturb")
        act_dnd.triggered.connect(lambda: self.on_toggle_dnd() if self.on_toggle_dnd else None)

        act_gaming = menu.addAction("Toggle Gaming Mode")
        act_gaming.triggered.connect(lambda: self.on_toggle_gaming() if self.on_toggle_gaming else None)

        menu.addSeparator()
        act_exit = menu.addAction("Exit Futaba")
        act_exit.triggered.connect(lambda: self.on_exit() if self.on_exit else QGuiApplication.quit())

        menu.exec(QCursor.pos())
