"""
Futaba UI Theme — Design tokens, color palette, styles, and icon utilities.

Purple Futaba aesthetic:
- Background: Deep Obsidian / Acrylic Dark (#12111A, #181624, #201D30)
- Surface / Cards: #242036, border: #3D3558
- Accent Primary: Futaba Purple (#7B2FBE, #9D4EDD)
- Accent Secondary: Bright Lavender (#C77DFF)
- Success: Emerald (#10B981)
- Warning / Waiting: Amber (#F59E0B)
- Blocked: Orange (#F97316)
- Error: Crimson (#EF4444)
- Text Primary: #F3F4F6
- Text Muted: #9CA3AF
"""

from __future__ import annotations

from PySide6.QtGui import QColor, QIcon, QPixmap, QPainter, QBrush, QPen, QRadialGradient, QLinearGradient
from PySide6.QtCore import Qt, QRectF, QPointF

# Colors
COLOR_BG_DARK = "#12111A"
COLOR_BG_SURFACE = "#181624"
COLOR_BG_CARD = "#221F33"
COLOR_BORDER = "#3B3356"
COLOR_BORDER_FOCUS = "#9D4EDD"

COLOR_ACCENT = "#7B2FBE"
COLOR_ACCENT_HOVER = "#9D4EDD"
COLOR_ACCENT_LIGHT = "#C77DFF"
COLOR_ACCENT_GLOW = "#E0AAFF"

COLOR_SUCCESS = "#10B981"
COLOR_WARNING = "#F59E0B"
COLOR_BLOCKED = "#F97316"
COLOR_ERROR = "#EF4444"
COLOR_INFO = "#3B82F6"

COLOR_TEXT_PRIMARY = "#F3F4F6"
COLOR_TEXT_SECONDARY = "#D1D5DB"
COLOR_TEXT_MUTED = "#9CA3AF"

# Global Stylesheet
MAIN_STYLESHEET = f"""
QMainWindow, QDialog, QWidget {{
    background-color: {COLOR_BG_SURFACE};
    color: {COLOR_TEXT_PRIMARY};
    font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
    font-size: 13px;
}}

/* Tab Bar */
QTabWidget::pane {{
    border: 1px solid {COLOR_BORDER};
    background-color: {COLOR_BG_DARK};
    border-radius: 8px;
    margin-top: -1px;
}}

QTabBar::tab {{
    background-color: {COLOR_BG_SURFACE};
    color: {COLOR_TEXT_MUTED};
    padding: 10px 18px;
    margin-right: 4px;
    border-top-left-radius: 6px;
    border-top-right-radius: 6px;
    border: 1px solid transparent;
    font-weight: 500;
}}

QTabBar::tab:selected {{
    background-color: {COLOR_BG_DARK};
    color: {COLOR_ACCENT_LIGHT};
    border: 1px solid {COLOR_BORDER};
    border-bottom: 1px solid {COLOR_BG_DARK};
    font-weight: 600;
}}

QTabBar::tab:hover:!selected {{
    background-color: {COLOR_BG_CARD};
    color: {COLOR_TEXT_PRIMARY};
}}

/* Push Buttons */
QPushButton {{
    background-color: {COLOR_ACCENT};
    color: white;
    border: none;
    border-radius: 6px;
    padding: 8px 16px;
    font-weight: 600;
}}

QPushButton:hover {{
    background-color: {COLOR_ACCENT_HOVER};
}}

QPushButton:pressed {{
    background-color: #5E1A9E;
}}

QPushButton:disabled {{
    background-color: #382F50;
    color: {COLOR_TEXT_MUTED};
}}

QPushButton.secondary {{
    background-color: {COLOR_BG_CARD};
    color: {COLOR_TEXT_PRIMARY};
    border: 1px solid {COLOR_BORDER};
}}

QPushButton.secondary:hover {{
    background-color: #2E2945;
    border-color: {COLOR_ACCENT};
}}

/* Inputs */
QLineEdit, QTextEdit, QPlainTextEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
    background-color: {COLOR_BG_DARK};
    color: {COLOR_TEXT_PRIMARY};
    border: 1px solid {COLOR_BORDER};
    border-radius: 6px;
    padding: 8px 12px;
    selection-background-color: {COLOR_ACCENT};
}}

QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus, QComboBox:focus {{
    border: 1px solid {COLOR_BORDER_FOCUS};
    background-color: #151320;
}}

/* Combo Box */
QComboBox::drop-down {{
    border: none;
    padding-right: 8px;
}}

QComboBox QAbstractItemView {{
    background-color: {COLOR_BG_SURFACE};
    border: 1px solid {COLOR_BORDER};
    selection-background-color: {COLOR_ACCENT};
    color: {COLOR_TEXT_PRIMARY};
}}

/* Tables and Lists */
QTableWidget, QTreeWidget, QListWidget {{
    background-color: {COLOR_BG_DARK};
    border: 1px solid {COLOR_BORDER};
    border-radius: 6px;
    gridline-color: {COLOR_BORDER};
    color: {COLOR_TEXT_PRIMARY};
}}

QHeaderView::section {{
    background-color: {COLOR_BG_SURFACE};
    color: {COLOR_TEXT_MUTED};
    padding: 8px;
    border: none;
    border-bottom: 1px solid {COLOR_BORDER};
    font-weight: 600;
}}

QTableWidget::item:selected, QListWidget::item:selected {{
    background-color: #3B2466;
    color: {COLOR_TEXT_PRIMARY};
}}

/* Scrollbars */
QScrollBar:vertical {{
    background: {COLOR_BG_DARK};
    width: 8px;
    margin: 0px;
    border-radius: 4px;
}}

QScrollBar::handle:vertical {{
    background: {COLOR_BORDER};
    min-height: 20px;
    border-radius: 4px;
}}

QScrollBar::handle:vertical:hover {{
    background: {COLOR_ACCENT};
}}

QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0px;
}}

/* Sliders */
QSlider::groove:horizontal {{
    height: 6px;
    background: {COLOR_BORDER};
    border-radius: 3px;
}}

QSlider::sub-page:horizontal {{
    background: {COLOR_ACCENT};
    border-radius: 3px;
}}

QSlider::handle:horizontal {{
    background: {COLOR_ACCENT_LIGHT};
    width: 14px;
    margin-top: -4px;
    margin-bottom: -4px;
    border-radius: 7px;
}}

/* Progress Bar */
QProgressBar {{
    background-color: {COLOR_BG_DARK};
    border: 1px solid {COLOR_BORDER};
    border-radius: 5px;
    text-align: center;
    color: {COLOR_TEXT_PRIMARY};
    font-size: 11px;
    font-weight: 600;
}}

QProgressBar::chunk {{
    background-color: {COLOR_ACCENT};
    border-radius: 4px;
}}

/* Group Box */
QGroupBox {{
    border: 1px solid {COLOR_BORDER};
    border-radius: 8px;
    margin-top: 14px;
    padding-top: 16px;
    font-weight: 600;
    color: {COLOR_ACCENT_LIGHT};
}}

QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    padding: 0 8px;
    left: 12px;
}}
"""


def create_futaba_orb_pixmap(size: int = 64, state: str = "idle") -> QPixmap:
    """
    Generate an in-memory high-res Futaba purple glowing orb icon.
    State changes the core accent color.
    """
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)

    center = QPointF(size / 2.0, size / 2.0)
    radius = size / 2.0 - 2.0

    state_colors = {
        "idle": (QColor(123, 47, 190), QColor(199, 125, 255)),
        "listening": (QColor(59, 130, 246), QColor(147, 197, 253)),
        "thinking": (QColor(168, 85, 247), QColor(233, 213, 255)),
        "working": (QColor(139, 92, 246), QColor(196, 181, 253)),
        "waiting": (QColor(245, 158, 11), QColor(253, 230, 138)),
        "blocked": (QColor(249, 115, 22), QColor(254, 215, 170)),
        "completed": (QColor(16, 185, 129), QColor(167, 243, 208)),
        "error": (QColor(239, 68, 68), QColor(254, 202, 202)),
    }

    c_primary, c_light = state_colors.get(state.lower(), state_colors["idle"])

    # Outer ambient glow
    glow_gradient = QRadialGradient(center, radius)
    glow_gradient.setColorAt(0.0, QColor(c_primary.red(), c_primary.green(), c_primary.blue(), 180))
    glow_gradient.setColorAt(0.7, QColor(c_primary.red(), c_primary.green(), c_primary.blue(), 60))
    glow_gradient.setColorAt(1.0, QColor(c_primary.red(), c_primary.green(), c_primary.blue(), 0))

    painter.setBrush(QBrush(glow_gradient))
    painter.setPen(Qt.NoPen)
    painter.drawEllipse(center, radius, radius)

    # Core sphere
    core_radius = radius * 0.72
    core_gradient = QRadialGradient(QPointF(center.x() - core_radius * 0.25, center.y() - core_radius * 0.25), core_radius)
    core_gradient.setColorAt(0.0, c_light)
    core_gradient.setColorAt(0.5, c_primary)
    core_gradient.setColorAt(1.0, QColor(18, 17, 26, 230))

    painter.setBrush(QBrush(core_gradient))
    painter.setPen(QPen(c_light, 1.2))
    painter.drawEllipse(center, core_radius, core_radius)

    # Stylized "F" inside orb
    painter.setPen(QPen(Qt.white, 2.0, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    f_w = core_radius * 0.45
    f_h = core_radius * 0.7
    top_left = QPointF(center.x() - f_w * 0.4, center.y() - f_h * 0.5)

    # Vertical bar
    painter.drawLine(top_left, QPointF(top_left.x(), top_left.y() + f_h))
    # Top horizontal bar
    painter.drawLine(top_left, QPointF(top_left.x() + f_w, top_left.y()))
    # Middle horizontal bar
    painter.drawLine(
        QPointF(top_left.x(), top_left.y() + f_h * 0.45),
        QPointF(top_left.x() + f_w * 0.75, top_left.y() + f_h * 0.45)
    )

    painter.end()
    return pixmap


def create_futaba_icon(state: str = "idle") -> QIcon:
    """Create a QIcon with the Futaba orb."""
    return QIcon(create_futaba_orb_pixmap(64, state))
