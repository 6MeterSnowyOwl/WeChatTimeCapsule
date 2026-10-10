from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication, QWidget


@dataclass(frozen=True)
class SemanticColors:
    TEXT_PRIMARY: str
    TEXT_SECONDARY: str
    TEXT_MUTED: str
    TEXT_DISABLED: str
    STATUS_SUCCESS: str
    STATUS_WARNING: str
    STATUS_ERROR: str
    STATUS_NEUTRAL: str
    SURFACE_BACKGROUND: str
    SURFACE_CARD: str
    SURFACE_ELEVATED: str
    BORDER: str
    ACCENT: str


class SemanticRole(str, Enum):
    TEXT_PRIMARY = "text-primary"
    TEXT_SECONDARY = "text-secondary"
    TEXT_MUTED = "text-muted"
    TEXT_DISABLED = "text-disabled"
    STATUS_SUCCESS = "status-success"
    STATUS_WARNING = "status-warning"
    STATUS_ERROR = "status-error"
    STATUS_NEUTRAL = "status-neutral"


DARK_COLORS = SemanticColors(
    TEXT_PRIMARY="#F4F7FB",
    TEXT_SECONDARY="#C9D2DF",
    TEXT_MUTED="#AAB5C4",
    TEXT_DISABLED="#7F8B9B",
    STATUS_SUCCESS="#69DB95",
    STATUS_WARNING="#F4C95D",
    STATUS_ERROR="#FF8A82",
    STATUS_NEUTRAL="#B8C2CF",
    SURFACE_BACKGROUND="#12161C",
    SURFACE_CARD="#1B222C",
    SURFACE_ELEVATED="#242D38",
    BORDER="#455366",
    ACCENT="#78A9FF",
)


def _linear_channel(channel: int) -> float:
    value = channel / 255.0
    return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4


def relative_luminance(color: str) -> float:
    value = QColor(color)
    if not value.isValid():
        raise ValueError(f"invalid color: {color}")
    red, green, blue, _ = value.getRgb()
    return (
        0.2126 * _linear_channel(red)
        + 0.7152 * _linear_channel(green)
        + 0.0722 * _linear_channel(blue)
    )


def contrast_ratio(foreground: str, background: str) -> float:
    light, dark = sorted(
        (relative_luminance(foreground), relative_luminance(background)),
        reverse=True,
    )
    return (light + 0.05) / (dark + 0.05)


def semantic_role(widget: QWidget, role: SemanticRole) -> None:
    widget.setProperty("semanticRole", role.value)
    style = widget.style()
    if style is not None:
        style.unpolish(widget)
        style.polish(widget)


def dark_palette(colors: SemanticColors = DARK_COLORS) -> QPalette:
    palette = QPalette()
    mappings = {
        QPalette.Window: colors.SURFACE_BACKGROUND,
        QPalette.WindowText: colors.TEXT_PRIMARY,
        QPalette.Base: colors.SURFACE_CARD,
        QPalette.AlternateBase: colors.SURFACE_ELEVATED,
        QPalette.ToolTipBase: colors.SURFACE_ELEVATED,
        QPalette.ToolTipText: colors.TEXT_PRIMARY,
        QPalette.Text: colors.TEXT_PRIMARY,
        QPalette.Button: colors.SURFACE_ELEVATED,
        QPalette.ButtonText: colors.TEXT_PRIMARY,
        QPalette.BrightText: colors.TEXT_PRIMARY,
        QPalette.Highlight: colors.ACCENT,
        QPalette.HighlightedText: colors.SURFACE_BACKGROUND,
        QPalette.PlaceholderText: colors.TEXT_MUTED,
        QPalette.Mid: colors.BORDER,
    }
    for role, color in mappings.items():
        palette.setColor(QPalette.Active, role, QColor(color))
        palette.setColor(QPalette.Inactive, role, QColor(color))
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        palette.setColor(QPalette.Disabled, role, QColor(colors.TEXT_DISABLED))
    palette.setColor(QPalette.Disabled, QPalette.Button, QColor(colors.SURFACE_ELEVATED))
    palette.setColor(QPalette.Disabled, QPalette.Base, QColor(colors.SURFACE_CARD))
    return palette


def application_style_sheet(colors: SemanticColors = DARK_COLORS) -> str:
    role_rules = "\n".join(
        f'QWidget[semanticRole="{role.value}"] {{ color: {getattr(colors, role.name)}; }}'
        for role in SemanticRole
    )
    return f"""
        QWidget {{ color: {colors.TEXT_PRIMARY}; font-size: 13px; }}
        {role_rules}
        QMainWindow, QDialog {{ background-color: {colors.SURFACE_BACKGROUND}; }}
        #mainTitle {{ font-size: 21px; font-weight: 600; }}
        #sectionTitle, #dialogHeading {{ font-size: 17px; font-weight: 600; }}
        #onboardingHeading {{ font-size: 23px; font-weight: 600; }}
        #cardName {{ font-size: 17px; font-weight: 600; }}
        #targetCard, #statusPanel {{
            border: 1px solid {colors.BORDER};
            border-radius: 7px;
            background-color: {colors.SURFACE_CARD};
        }}
        #targetCard:focus {{ border: 2px solid {colors.ACCENT}; }}
        #cardHeader, #cardActions, #cardAvatar {{ border: none; background: transparent; }}
        #statusDetail {{ border: none; background: transparent; }}
        #cardArrow {{ font-size: 22px; padding: 0; border: none; background: transparent; }}
        QPushButton {{
            color: {colors.TEXT_PRIMARY};
            background-color: {colors.SURFACE_ELEVATED};
            border: 1px solid {colors.BORDER};
            border-radius: 5px;
            padding: 5px 10px;
        }}
        QPushButton:hover {{ border-color: {colors.ACCENT}; }}
        QPushButton:disabled {{ color: {colors.TEXT_DISABLED}; border-color: {colors.BORDER}; }}
        #primaryButton {{ font-weight: 600; padding: 7px 16px; }}
        #dangerButton {{ color: {colors.STATUS_ERROR}; border-color: {colors.STATUS_ERROR}; }}
        #dangerButton:hover {{ background-color: {colors.SURFACE_ELEVATED}; }}
        #removeTargetButton {{ color: {colors.STATUS_WARNING}; }}
        QLineEdit, QListWidget, QScrollArea {{
            color: {colors.TEXT_PRIMARY};
            background-color: {colors.SURFACE_CARD};
            border-color: {colors.BORDER};
        }}
    """


def apply_application_theme(app: QApplication) -> None:
    app.setStyle("Fusion")
    app.setPalette(dark_palette())
    app.setStyleSheet(application_style_sheet())


def apply_widget_theme(widget: QWidget) -> None:
    widget.setPalette(dark_palette())
    widget.setStyleSheet(application_style_sheet())
