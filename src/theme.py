"""Design tokens and the glass treatment for the chat UI.

Two things live here. First, a small set of tokens — type scale, spacing, radii,
colour — so every surface is built from the same vocabulary instead of ad-hoc
hex codes scattered through the widgets. Second, the Windows 11 backdrop calls
that make the glass real: DWM composites acrylic behind the window, which is why
it looks like frosted glass instead of a flat grey rectangle pretending to be
one.

Everything degrades: on an older Windows the DWM calls fail quietly and the
stylesheet's own translucent fills carry the look on their own.
"""
from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

from PySide6.QtWidgets import QGraphicsDropShadowEffect, QWidget
from PySide6.QtGui import QColor

# --- Type -------------------------------------------------------------------
# Segoe UI Variable is the Windows 11 UI face and is noticeably better spaced at
# small sizes than plain Segoe UI; the rest are fallbacks in preference order.
FONT_STACK = '"Segoe UI Variable Text", "Segoe UI", Inter, "Helvetica Neue", Arial'
FONT_DISPLAY = '"Segoe UI Variable Display", "Segoe UI Semibold", "Segoe UI"'

PT_DISPLAY = 21
PT_TITLE = 12
PT_BODY = 11
PT_SMALL = 10
PT_CAPTION = 9

# --- Space and shape --------------------------------------------------------
SPACE_XS, SPACE_SM, SPACE_MD, SPACE_LG, SPACE_XL = 4, 8, 12, 16, 24
RADIUS_SM, RADIUS_MD, RADIUS_LG, RADIUS_PILL = 8, 12, 18, 999

# --- Colour -----------------------------------------------------------------
# Surfaces are deliberately low-alpha whites: over an acrylic backdrop they read
# as depth, not as grey paint.
INK = "#F3F4F7"
INK_SOFT = "#AEB4C2"
INK_FAINT = "#767D8C"

GLASS_BASE = "rgba(16, 18, 26, 176)"
GLASS_RAISED = "rgba(255, 255, 255, 12)"
GLASS_HOVER = "rgba(255, 255, 255, 26)"
GLASS_SUNK = "rgba(8, 9, 14, 120)"
STROKE = "rgba(255, 255, 255, 28)"
STROKE_SOFT = "rgba(255, 255, 255, 16)"

ACCENT = "#4C8DFF"
ACCENT_DEEP = "#6E5BFF"
ACCENT_SOFT = "rgba(76, 141, 255, 46)"
LIVE = "#FF6B5F"
GOOD = "#7BE0A6"
WARN = "#FFCF6B"

ACCENT_GRADIENT = (
    f"qlineargradient(x1:0, y1:0, x2:1, y2:1, "
    f"stop:0 {ACCENT}, stop:1 {ACCENT_DEEP})"
)
ACCENT_GRADIENT_HOVER = (
    "qlineargradient(x1:0, y1:0, x2:1, y2:1, "
    "stop:0 #5F9BFF, stop:1 #8070FF)"
)


# --- Windows 11 glass -------------------------------------------------------
_DWMWA_USE_IMMERSIVE_DARK_MODE = 20
_DWMWA_WINDOW_CORNER_PREFERENCE = 33
_DWMWA_SYSTEMBACKDROP_TYPE = 38

_CORNER_ROUND = 2
_CORNER_ROUND_SMALL = 3
_BACKDROP_ACRYLIC = 3  # DWMSBT_TRANSIENTWINDOW


def _set_attribute(hwnd: int, attribute: int, value: int) -> bool:
    try:
        result = ctypes.windll.dwmapi.DwmSetWindowAttribute(
            wintypes.HWND(hwnd),
            wintypes.DWORD(attribute),
            ctypes.byref(ctypes.c_int(value)),
            ctypes.sizeof(ctypes.c_int),
        )
        return result == 0
    except Exception:  # noqa: BLE001 - any missing export means "no glass here"
        return False


def apply_glass(widget: QWidget, small_corners: bool = False) -> bool:
    """Ask DWM for an acrylic backdrop, dark titlebar and rounded corners.

    Returns whether the backdrop was accepted, so callers can fall back to a
    heavier opaque fill when the compositor will not do the work for them.
    """
    if sys.platform != "win32":
        return False
    hwnd = int(widget.winId())
    _set_attribute(hwnd, _DWMWA_USE_IMMERSIVE_DARK_MODE, 1)
    _set_attribute(
        hwnd,
        _DWMWA_WINDOW_CORNER_PREFERENCE,
        _CORNER_ROUND_SMALL if small_corners else _CORNER_ROUND,
    )
    return _set_attribute(hwnd, _DWMWA_SYSTEMBACKDROP_TYPE, _BACKDROP_ACRYLIC)


def elevate(widget: QWidget, blur: int = 38, alpha: int = 150, dy: int = 8) -> None:
    """Lift a surface off the background with a soft shadow.

    Qt stylesheets have no box-shadow, so elevation has to be an effect. Used
    sparingly: only on things that genuinely float.
    """
    shadow = QGraphicsDropShadowEffect(widget)
    shadow.setBlurRadius(blur)
    shadow.setColor(QColor(0, 0, 0, alpha))
    shadow.setOffset(0, dy)
    widget.setGraphicsEffect(shadow)


def stylesheet(own_corners: bool = False) -> str:
    """The whole chat surface, built from the tokens above.

    own_corners rounds the shell in CSS. Leave it off when DWM accepted the
    acrylic backdrop: DWM fills the entire window rect and rounds it itself, so
    a second radius on top only carves grey wedges out of the corners where our
    rounding and the compositor's disagree.
    """
    shell_radius = f"{RADIUS_LG}px" if own_corners else "0px"
    return f"""
        QWidget {{
            background: transparent;
            color: {INK};
            font-family: {FONT_STACK};
            font-size: {PT_BODY}pt;
        }}

        /* ---- Shell ---- */
        #shell {{
            background-color: {GLASS_BASE};
            border: 1px solid {STROKE};
            border-radius: {shell_radius};
        }}
        #sidebar {{
            background-color: rgba(255, 255, 255, 8);
            border-right: 1px solid {STROKE_SOFT};
            border-bottom-left-radius: {shell_radius};
        }}
        #mainarea {{ background: transparent; }}

        /* ---- Header ---- */
        #brand {{
            font-family: {FONT_DISPLAY};
            font-size: {PT_TITLE}pt;
            font-weight: 600;
            color: {INK};
        }}
        #windowbtn, #closebtn {{
            background: transparent;
            color: {INK_SOFT};
            border: none;
            border-radius: {RADIUS_SM}px;
            font-size: {PT_SMALL}pt;
        }}
        #windowbtn:hover {{
            background-color: {GLASS_HOVER};
            color: {INK};
        }}
        #closebtn:hover {{
            background-color: rgba(255, 107, 95, 190);
            color: #ffffff;
        }}

        /* ---- Type roles ---- */
        #sectionlabel {{
            color: {INK_FAINT};
            font-size: {PT_CAPTION}pt;
            font-weight: 600;
            letter-spacing: 0.6px;
            text-transform: uppercase;
        }}
        #chiplabel {{ color: {INK_SOFT}; font-size: {PT_CAPTION}pt; }}
        #status {{ color: {INK_FAINT}; font-size: {PT_CAPTION}pt; }}
        #usage {{ color: {GOOD}; font-size: {PT_CAPTION}pt; }}
        #emptytitle {{
            font-family: {FONT_DISPLAY};
            font-size: {PT_DISPLAY}pt;
            font-weight: 600;
            color: {INK};
        }}
        #emptyhint {{ color: {INK_FAINT}; font-size: {PT_SMALL}pt; }}

        /* ---- Buttons ---- */
        QPushButton#primary {{
            background: {ACCENT_GRADIENT};
            color: #ffffff;
            border: none;
            border-radius: {RADIUS_MD}px;
            font-size: {PT_BODY}pt;
            font-weight: 600;
            padding: 0 18px;
        }}
        QPushButton#primary:hover {{ background: {ACCENT_GRADIENT_HOVER}; }}
        QPushButton#primary:pressed {{ background: {ACCENT_DEEP}; }}
        QPushButton#primary:disabled {{
            background: rgba(255, 255, 255, 20);
            color: {INK_FAINT};
        }}

        QPushButton#ghost {{
            background-color: {GLASS_RAISED};
            color: {INK};
            border: 1px solid {STROKE};
            border-radius: {RADIUS_MD}px;
            font-size: {PT_SMALL}pt;
            padding: 0 14px;
        }}
        QPushButton#ghost:hover {{
            background-color: {GLASS_HOVER};
            border-color: rgba(255, 255, 255, 52);
        }}

        QPushButton#newchat {{
            background-color: {GLASS_RAISED};
            color: {INK};
            border: 1px solid {STROKE};
            border-radius: {RADIUS_MD}px;
            font-size: {PT_SMALL}pt;
            font-weight: 600;
            text-align: left;
            padding-left: 14px;
        }}
        QPushButton#newchat:hover {{
            background-color: {ACCENT_SOFT};
            border-color: rgba(76, 141, 255, 120);
        }}

        /* ---- Inputs ---- */
        QLineEdit#search {{
            background-color: {GLASS_SUNK};
            color: {INK};
            border: 1px solid {STROKE_SOFT};
            border-radius: {RADIUS_PILL}px;
            padding: 8px 14px;
            font-size: {PT_SMALL}pt;
        }}
        QLineEdit#search:focus {{ border-color: {ACCENT}; }}

        QTextEdit#contextbox {{
            background-color: {GLASS_SUNK};
            color: {INK};
            border: 1px solid {STROKE_SOFT};
            border-radius: {RADIUS_MD}px;
            padding: 10px 12px;
            font-size: {PT_SMALL}pt;
            selection-background-color: {ACCENT};
        }}
        QTextEdit#contextbox:focus {{ border-color: rgba(76, 141, 255, 140); }}

        QComboBox#chip {{
            background-color: {GLASS_RAISED};
            color: {INK};
            border: 1px solid {STROKE};
            border-radius: {RADIUS_PILL}px;
            padding: 7px 14px;
            font-size: {PT_CAPTION}pt;
        }}
        QComboBox#chip:hover {{ background-color: {GLASS_HOVER}; }}
        QComboBox#chip::drop-down {{ border: none; width: 18px; }}
        QComboBox#chip QAbstractItemView {{
            background-color: #171A24;
            color: {INK};
            border: 1px solid {STROKE};
            border-radius: {RADIUS_SM}px;
            padding: 4px;
            selection-background-color: {ACCENT};
            outline: none;
        }}

        /* ---- Composer ---- */
        #composer {{
            background-color: rgba(255, 255, 255, 10);
            border: 1px solid {STROKE};
            border-radius: {RADIUS_LG}px;
        }}

        /* ---- Lists ---- */
        QListWidget#historylist {{
            background: transparent;
            border: none;
            color: {INK_SOFT};
            font-size: {PT_SMALL}pt;
            outline: none;
        }}
        QListWidget#historylist::item {{
            padding: 8px 10px;
            border-radius: {RADIUS_SM}px;
            margin-bottom: 2px;
        }}
        QListWidget#historylist::item:hover {{ background-color: {GLASS_RAISED}; }}
        QListWidget#historylist::item:selected {{
            background-color: {ACCENT_SOFT};
            color: {INK};
        }}

        QSplitter#sidesplit::handle {{ background: transparent; }}

        /* The transcript/composer divider has to look draggable: it is how the
           prompt area gets made bigger. */
        QSplitter#bodysplit::handle {{
            background: transparent;
            image: none;
            border-top: 2px solid rgba(255, 255, 255, 20);
            margin: 4px 40%;
            border-radius: 1px;
        }}
        QSplitter#bodysplit::handle:hover {{
            border-top: 2px solid {ACCENT};
        }}

        /* ---- Scrollbars ---- */
        QScrollBar:vertical {{
            background: transparent; width: 10px; margin: 2px;
        }}
        QScrollBar::handle:vertical {{
            background: rgba(255, 255, 255, 34);
            border-radius: 5px;
            min-height: 36px;
        }}
        QScrollBar::handle:vertical:hover {{ background: rgba(255, 255, 255, 60); }}
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
        QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
            background: transparent;
        }}

        QToolTip {{
            background-color: #171A24;
            color: {INK};
            border: 1px solid {STROKE};
            border-radius: {RADIUS_SM}px;
            padding: 6px 8px;
        }}
    """


def panel_stylesheet(own_corners: bool = False) -> str:
    """The floating Live panel. Same vocabulary, tuned for a small surface."""
    panel_radius = f"{RADIUS_LG}px" if own_corners else "0px"
    return f"""
        QWidget {{
            background: transparent;
            color: {INK};
            font-family: {FONT_STACK};
            font-size: {PT_SMALL}pt;
        }}
        #panelcard {{
            background-color: rgba(14, 16, 24, 190);
            border: 1px solid {STROKE};
            border-radius: {panel_radius};
        }}
        #panelstate {{
            color: {WARN};
            font-size: {PT_CAPTION}pt;
            font-weight: 600;
            letter-spacing: 0.3px;
        }}
        #panelheard {{
            color: #BBD1FF;
            font-size: {PT_SMALL}pt;
            background-color: {GLASS_SUNK};
            border: 1px solid {STROKE_SOFT};
            border-radius: {RADIUS_MD}px;
            padding: 9px 11px;
        }}
        QTextEdit#panelanswer {{
            background-color: rgba(255, 255, 255, 14);
            color: {INK};
            border: 1px solid {STROKE_SOFT};
            border-radius: {RADIUS_MD}px;
            padding: 10px 12px;
            font-size: {PT_BODY}pt;
            selection-background-color: {ACCENT};
        }}
        QTextEdit#paneltranslation {{
            background-color: rgba(123, 224, 166, 24);
            color: {GOOD};
            border: none;
            border-radius: {RADIUS_MD}px;
            padding: 8px 11px;
            font-size: {PT_SMALL}pt;
        }}
        QPushButton#panelaction {{
            background: {ACCENT_GRADIENT};
            color: #ffffff;
            border: none;
            border-radius: {RADIUS_MD}px;
            font-size: {PT_SMALL}pt;
            font-weight: 600;
        }}
        QPushButton#panelaction:hover {{ background: {ACCENT_GRADIENT_HOVER}; }}
        QPushButton#panelaction:disabled {{
            background: rgba(255, 255, 255, 20);
            color: {INK_FAINT};
        }}
        QPushButton#panelghost {{
            background-color: {GLASS_RAISED};
            color: {INK};
            border: 1px solid {STROKE};
            border-radius: {RADIUS_MD}px;
            font-size: {PT_CAPTION}pt;
            padding: 0 12px;
        }}
        QPushButton#panelghost:hover {{ background-color: {GLASS_HOVER}; }}
        QPushButton#panelclose {{
            background: transparent;
            color: {INK_FAINT};
            border: none;
            border-radius: {RADIUS_SM}px;
            font-size: {PT_CAPTION}pt;
        }}
        QPushButton#panelclose:hover {{
            background-color: rgba(255, 107, 95, 190);
            color: #ffffff;
        }}
        QScrollBar:vertical {{
            background: transparent; width: 8px; margin: 4px 2px;
        }}
        QScrollBar::handle:vertical {{
            background: rgba(255, 255, 255, 40);
            border-radius: 4px;
            min-height: 28px;
        }}
        QScrollBar::handle:vertical:hover {{ background: rgba(255, 255, 255, 70); }}
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
        QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
            background: transparent;
        }}
    """


def bubble_style(role: str) -> tuple[str, str, str]:
    """Background, text colour and prefix for a transcript bubble."""
    return {
        "question": (ACCENT_SOFT, "#CFE0FF", "❓  "),
        "answer": ("rgba(255, 255, 255, 13)", INK, ""),
        "translation": ("rgba(123, 224, 166, 20)", GOOD, "👁  "),
        "system": ("transparent", INK_FAINT, ""),
    }.get(role, ("transparent", INK_FAINT, ""))
