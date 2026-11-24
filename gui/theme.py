"""dark theme and the colors shared by the GUI widgets (green / yellow / red as in the TUI)."""

from __future__ import annotations

from typing import Literal

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

from formatting import Level

# a muted dark palette (not pure black / white, which glares on large panels)
DARK_PALETTE: dict[QPalette.ColorRole, str] = {
    QPalette.ColorRole.Window: "#1c2128",
    QPalette.ColorRole.WindowText: "#adbac7",
    QPalette.ColorRole.Base: "#22272e",
    QPalette.ColorRole.AlternateBase: "#272d35",
    QPalette.ColorRole.Text: "#adbac7",
    QPalette.ColorRole.PlaceholderText: "#636e7b",
    QPalette.ColorRole.Button: "#2d333b",
    QPalette.ColorRole.ButtonText: "#adbac7",
    QPalette.ColorRole.BrightText: "#cdd9e5",
    QPalette.ColorRole.Highlight: "#316dca",
    QPalette.ColorRole.HighlightedText: "#ffffff",
    QPalette.ColorRole.ToolTipBase: "#2d333b",
    QPalette.ColorRole.ToolTipText: "#adbac7",
    QPalette.ColorRole.Link: "#539bf5",
    QPalette.ColorRole.Light: "#444c56",
    QPalette.ColorRole.Midlight: "#373e47",
    QPalette.ColorRole.Mid: "#2d333b",
    QPalette.ColorRole.Dark: "#1c2128",
    QPalette.ColorRole.Shadow: "#101317",
}
DISABLED_TEXT = "#545d68"

LEVEL_COLORS: dict[Level, str] = {
    "green": "#2ea043",
    "yellow": "#d29922",
    "red": "#e5534b",
}
ACCENT_COLOR = "#539bf5"  # chart lines, like the TUI's primary color
BLOCK_NUMBER_COLOR = "#39c5cf"  # the TUI shows block numbers in cyan
EVENT_NAME_COLOR = "#b083f0"  # decoded event names, magenta in the TUI
PAUSED_COLOR = LEVEL_COLORS["yellow"]
PAUSED_TEXT_COLOR = "#1c2128"  # dark text on the yellow "paused" badge, as in the TUI
DIM_MIX = 0.5  # share of the background mixed into the text color for dim text


def apply_dark_theme(app: QApplication) -> None:
    """use the Fusion style with the dark palette; call before creating any widget."""
    app.setStyle("Fusion")  # native styles may ignore parts of a custom palette
    palette = QPalette()
    for role, color in DARK_PALETTE.items():
        palette.setColor(role, QColor(color))
    for role in (
        QPalette.ColorRole.WindowText,
        QPalette.ColorRole.Text,
        QPalette.ColorRole.ButtonText,
    ):
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor(DISABLED_TEXT))
    app.setPalette(palette)


def level_color(level: Level) -> str:
    return LEVEL_COLORS[level]


def palette_color(role: Literal["base", "text"]) -> str:
    """background of views ("base") or window text color from the current palette."""
    qt_role = QPalette.ColorRole.Base if role == "base" else QPalette.ColorRole.WindowText
    return QApplication.palette().color(qt_role).name()


def dim_color() -> str:
    """muted text color from the current palette, like the TUI's dim style."""
    palette = QApplication.palette()
    text = palette.color(QPalette.ColorRole.WindowText)
    background = palette.color(QPalette.ColorRole.Window)

    def mix(fg: int, bg: int) -> int:
        return round(fg + (bg - fg) * DIM_MIX)

    return QColor(
        mix(text.red(), background.red()),
        mix(text.green(), background.green()),
        mix(text.blue(), background.blue()),
    ).name()
