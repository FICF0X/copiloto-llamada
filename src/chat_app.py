"""Chat-style redesign of CallAssist (prototype).

Two surfaces instead of one crowded overlay:

- A full chat window where the call is set up and the whole conversation is read
  back as message bubbles. Setup lives here so nothing competes for space during
  the call.
- A small always-on-top Live panel that appears while listening. It shows what is
  being heard in real time, sends on one click, and streams the answer right
  there — so the answer is read where the eyes already are, without switching
  windows.

Both surfaces render the same worker signals, so the chat stays a complete record
of whatever happened in the panel.

Run with:  python -m src.chat_app
"""
from __future__ import annotations

import ctypes
import sys

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QIcon,
    QKeySequence,
    QLinearGradient,
    QPainter,
    QShortcut,
    QTextCursor,
)
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizeGrip,
    QSplitter,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from src import conversations, theme
from src.audio_capture import list_loopback_devices
from src.brain import Brain
from src.config import (
    CONTEXT_FILE,
    DEVICE_FILE,
    HIDE_FILE,
    HIDE_FROM_SCREENSHARE,
    LENGTH_FILE,
    MODE_FILE,
    ROOT,
    USAGE_FILE,
)
from src.listener import Listener
from src.transcriber import Transcriber
from src.translator import Translator
from src.usage import UsageTracker
from src.worker import CopilotWorker

# Windows: keep the window visible locally but absent from screen capture and
# screen sharing. Requires Windows 10 2004+.
WDA_EXCLUDEFROMCAPTURE = 0x00000011
WDA_NONE = 0x00000000

# Geometry of each surface, remembered separately: they are moved and sized for
# different jobs (reading vs. glancing).
CHAT_GEOMETRY_FILE = ROOT / "chat_geometry.txt"
PANEL_GEOMETRY_FILE = ROOT / "panel_geometry.txt"
# How the transcript/composer split was last dragged, so a prompt the user made
# room for stays roomy on the next launch.
SPLIT_FILE = ROOT / "composer_split.txt"
EDITOR_GEOMETRY_FILE = ROOT / "prompt_editor_geometry.txt"

PANEL_DEFAULT = (60, 60, 460, 340)


def _read_geometry(path, fallback: tuple[int, int, int, int] | None = None):
    try:
        x, y, w, h = (int(v) for v in path.read_text(encoding="utf-8").split(","))
        return x, y, w, h
    except (OSError, ValueError):
        return fallback


def _write_geometry(path, widget: QWidget) -> None:
    geo = widget.geometry()
    try:
        path.write_text(
            f"{geo.x()},{geo.y()},{geo.width()},{geo.height()}", encoding="utf-8"
        )
    except OSError:
        pass


def _read_text(path, fallback: str = "") -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return fallback


def _write_text(path, value: str) -> None:
    try:
        path.write_text(value, encoding="utf-8")
    except OSError:
        pass


def _load_stealth_setting() -> bool:
    """First run has no HIDE_FILE yet, so the config constant is the default."""
    raw = _read_text(HIDE_FILE).strip()
    if raw in ("1", "0"):
        return raw == "1"
    return HIDE_FROM_SCREENSHARE


# Whether app windows are currently hidden from screen capture. Read once at
# startup and flipped at runtime by the "Ocultar al compartir pantalla"
# toggle; every window re-applies this value the moment it changes, and again
# on its own showEvent so a window shown later still respects it.
_stealth_enabled = _load_stealth_setting()


def _apply_stealth(widget: QWidget) -> None:
    """Hide (or restore) the window from screen capture, per the current toggle."""
    if sys.platform != "win32":
        return
    affinity = WDA_EXCLUDEFROMCAPTURE if _stealth_enabled else WDA_NONE
    try:
        ctypes.windll.user32.SetWindowDisplayAffinity(int(widget.winId()), affinity)
    except Exception:  # noqa: BLE001 - stealth is best-effort
        pass


def _apply_app_icon(widget: QWidget) -> None:
    """Set this window's own icon explicitly.

    QApplication.setWindowIcon() is supposed to reach every top-level widget,
    but frameless/tool windows do not reliably pick it up for the taskbar —
    setting it on the instance directly makes sure they do.
    """
    app = QApplication.instance()
    if app is not None and not app.windowIcon().isNull():
        widget.setWindowIcon(app.windowIcon())


# --- Frameless resize -------------------------------------------------------
# Frameless windows have no native resize border, so dragging their edges does
# nothing by default. A grip widget per edge/corner hands off to the OS via
# QWindow.startSystemResize the moment it is pressed — the same idea as the
# QSizeGrip already used for the bottom-right corner, just covering every
# edge instead of only one.
RESIZE_MARGIN = 8

_EDGE_CURSORS = {
    Qt.LeftEdge: Qt.SizeHorCursor,
    Qt.RightEdge: Qt.SizeHorCursor,
    Qt.TopEdge: Qt.SizeVerCursor,
    Qt.BottomEdge: Qt.SizeVerCursor,
    Qt.TopEdge | Qt.LeftEdge: Qt.SizeFDiagCursor,
    Qt.TopEdge | Qt.RightEdge: Qt.SizeBDiagCursor,
    Qt.BottomEdge | Qt.LeftEdge: Qt.SizeBDiagCursor,
    Qt.BottomEdge | Qt.RightEdge: Qt.SizeFDiagCursor,
}


class _ResizeGrip(QWidget):
    """Invisible strip covering one edge or corner of a frameless window."""

    def __init__(self, window: QWidget, edges: Qt.Edges) -> None:
        super().__init__(window)
        self._window = window
        self._edges = edges
        self.setCursor(_EDGE_CURSORS[edges])

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            handle = self._window.windowHandle()
            if handle is not None:
                handle.startSystemResize(self._edges)


def _add_resize_grips(window: QWidget):
    """Attach edge/corner grips to `window`; return a fn that repositions them.

    Call the returned function from the window's resizeEvent so the grips
    keep hugging the border as it grows or shrinks.
    """
    m = RESIZE_MARGIN
    grips = [_ResizeGrip(window, edges) for edges in _EDGE_CURSORS]
    left, right, top, bottom, tl, tr, bl, br = grips

    def reposition() -> None:
        w, h = window.width(), window.height()
        left.setGeometry(0, m, m, max(h - 2 * m, 0))
        right.setGeometry(w - m, m, m, max(h - 2 * m, 0))
        top.setGeometry(m, 0, max(w - 2 * m, 0), m)
        bottom.setGeometry(m, h - m, max(w - 2 * m, 0), m)
        tl.setGeometry(0, 0, m, m)
        tr.setGeometry(w - m, 0, m, m)
        bl.setGeometry(0, h - m, m, m)
        br.setGeometry(w - m, h - m, m, m)
        for g in grips:
            g.raise_()

    reposition()
    return reposition


class ToggleSwitch(QCheckBox):
    """A sliding ON/OFF switch. Behaves like a checkbox but looks like a toggle."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(46, 24)

    def hitButton(self, pos) -> bool:
        # A plain QCheckBox only reacts on its tiny left indicator; make the WHOLE
        # painted switch clickable so tapping the right side toggles too.
        return self.rect().contains(pos)

    def paintEvent(self, event) -> None:  # noqa: ARG002 - Qt signature
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)

        radius = self.height() / 2
        if self.isChecked():
            track = QLinearGradient(0, 0, self.width(), self.height())
            track.setColorAt(0.0, QColor(theme.ACCENT))
            track.setColorAt(1.0, QColor(theme.ACCENT_DEEP))
            painter.setBrush(track)
        else:
            painter.setBrush(QColor(255, 255, 255, 40))
        painter.drawRoundedRect(0, 0, self.width(), self.height(), radius, radius)

        # White knob slides to the right when ON, left when OFF.
        diameter = self.height() - 6
        x = self.width() - diameter - 3 if self.isChecked() else 3
        painter.setBrush(QColor("#ffffff"))
        painter.drawEllipse(int(x), 3, diameter, diameter)


class DragBar(QWidget):
    """Header strip that moves its frameless window when dragged."""

    def __init__(self, window: QWidget) -> None:
        super().__init__()
        self._window = window
        self._offset = None
        self.setCursor(Qt.SizeAllCursor)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self._offset = (
                event.globalPosition().toPoint() - self._window.frameGeometry().topLeft()
            )

    def mouseMoveEvent(self, event) -> None:
        if self._offset and event.buttons() & Qt.LeftButton:
            self._window.move(event.globalPosition().toPoint() - self._offset)

    def mouseReleaseEvent(self, event) -> None:
        self._offset = None


class Bubble(QFrame):
    """One message in the transcript. Role decides colour and alignment."""

    def __init__(self, role: str, text: str, font_pt: int = theme.PT_BODY) -> None:
        super().__init__()
        bg, fg, prefix = theme.bubble_style(role)
        self.setObjectName(f"bubble_{role}")
        border = (
            f"1px solid {theme.STROKE_SOFT}" if role != "system" else "none"
        )
        self.setStyleSheet(
            f"#bubble_{role} {{ background-color: {bg};"
            f" border: {border}; border-radius: {theme.RADIUS_MD}px; }}"
        )
        lay = QVBoxLayout(self)
        lay.setContentsMargins(15, 12, 15, 12)
        self.label = QLabel(prefix + text)
        self.label.setWordWrap(True)
        self.label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.label.setFont(QFont("Segoe UI Variable Text", font_pt))
        # No line-height here: Qt paints it but sizes the label without it, so the
        # bubble ends up shorter than its own text and clips the last lines.
        self.label.setStyleSheet(f"color: {fg}; background: transparent;")
        self._prefix = prefix
        self._raw = text  # text without the prefix, so appends stay exact
        lay.addWidget(self.label)
        self._fit()

    def _fit(self) -> None:
        """Size the bubble to exactly the height its wrapped text needs.

        A word-wrapped QLabel only reports a useful height via heightForWidth,
        which QFrame does not propagate, so inside a scroll area the bubble keeps
        whatever height it had when it was first laid out. That is fine for text
        set once and wrong for a streamed answer: the bubble stays at its empty
        height and silently clips everything that arrives after.
        """
        margins = self.layout().contentsMargins()
        inner = max(self.width() - margins.left() - margins.right(), 1)
        height = self.label.heightForWidth(inner)
        self.label.setFixedHeight(height)
        self.setFixedHeight(height + margins.top() + margins.bottom())

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._fit()  # rewrapping on a width change changes the line count

    def set_text(self, text: str) -> None:
        self._raw = text
        self.label.setText(self._prefix + text)
        self._fit()

    def append(self, text: str) -> None:
        self.set_text(self._raw + text)


class ChatView(QScrollArea):
    """Scrolling transcript of bubbles that follows the newest message."""

    def __init__(self) -> None:
        super().__init__()
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        self._body = QWidget()
        self._lay = QVBoxLayout(self._body)
        self._lay.setContentsMargins(16, 16, 16, 16)
        self._lay.setSpacing(10)

        self._empty = QWidget()
        empty_lay = QVBoxLayout(self._empty)
        empty_lay.setContentsMargins(0, 90, 0, 0)
        empty_lay.setSpacing(theme.SPACE_SM)
        headline = QLabel("¿Por dónde empezamos?")
        headline.setObjectName("emptytitle")
        headline.setAlignment(Qt.AlignCenter)
        hint = QLabel(
            "Escribe el contexto de la reunión abajo y pulsa Escuchar.\n"
            "La ventana flotante te muestra lo que oye y la respuesta."
        )
        hint.setObjectName("emptyhint")
        hint.setAlignment(Qt.AlignCenter)
        empty_lay.addWidget(headline)
        empty_lay.addWidget(hint)
        self._lay.addWidget(self._empty)
        self._lay.addStretch()

        self.setWidget(self._body)
        self._font_pt = theme.PT_BODY
        self._live_answer: Bubble | None = None
        self._live_translation: Bubble | None = None
        self._anchors: list[QWidget] = []  # first bubble of each exchange

    def _add(self, widget: QWidget) -> None:
        if self._empty is not None:
            self._empty.setVisible(False)
        # Insert before the trailing stretch so bubbles stack from the top.
        self._lay.insertWidget(self._lay.count() - 1, widget)
        QTimer.singleShot(0, self._scroll_to_bottom)

    def _scroll_to_bottom(self) -> None:
        bar = self.verticalScrollBar()
        bar.setValue(bar.maximum())

    def set_font_pt(self, pt: int) -> None:
        self._font_pt = pt
        for i in range(self._lay.count()):
            item = self._lay.itemAt(i).widget()
            if isinstance(item, Bubble):
                item.label.setFont(QFont("Segoe UI Variable Text", pt))

    def add_question(self, text: str) -> None:
        bubble = Bubble("question", text, self._font_pt)
        self._add(bubble)
        self._anchors.append(bubble)
        # A fresh answer bubble is created empty so chunks have somewhere to land.
        self._live_answer = Bubble("answer", "", self._font_pt)
        self._add(self._live_answer)
        self._live_translation = None

    def append_answer(self, text: str) -> None:
        if self._live_answer is None:
            self._live_answer = Bubble("answer", "", self._font_pt)
            self._add(self._live_answer)
        self._live_answer.append(text)
        self._scroll_to_bottom()

    def set_translation(self, text: str) -> None:
        if self._live_translation is None:
            self._live_translation = Bubble("translation", text, self._font_pt)
            self._add(self._live_translation)
        else:
            self._live_translation.set_text(text)

    def add_system(self, text: str) -> None:
        self._add(Bubble("system", text, self._font_pt))

    def scroll_to_exchange(self, index: int) -> None:
        if 0 <= index < len(self._anchors):
            self.ensureWidgetVisible(self._anchors[index], 0, 40)

    def clear(self) -> None:
        while self._lay.count() > 1:  # keep the trailing stretch
            item = self._lay.takeAt(0)
            widget = item.widget()
            if widget is not None and widget is not self._empty:
                widget.deleteLater()
        self._lay.insertWidget(0, self._empty)
        self._empty.setVisible(True)
        self._live_answer = None
        self._live_translation = None
        self._anchors = []


class LivePanel(QWidget):
    """Small always-on-top window used during the call.

    Everything needed mid-call and nothing else: what is being heard, one button
    to send, and the answer streaming in place.
    """

    send_requested = Signal()
    listen_requested = Signal()
    cancel_requested = Signal()
    closed = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
        )
        self.setMinimumSize(320, 220)
        _apply_app_icon(self)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        card = QWidget()
        card.setObjectName("panelcard")
        root.addWidget(card)

        lay = QVBoxLayout(card)
        lay.setContentsMargins(12, 8, 12, 10)
        lay.setSpacing(8)

        # --- Header: drag handle + state + close ---
        header = DragBar(self)
        hlay = QHBoxLayout(header)
        hlay.setContentsMargins(0, 0, 0, 0)
        self.state_label = QLabel("🎧 En vivo")
        self.state_label.setObjectName("panelstate")
        close_btn = QPushButton("✕")
        close_btn.setObjectName("panelclose")
        close_btn.setFixedSize(22, 22)
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.clicked.connect(self.closed.emit)
        hlay.addWidget(self.state_label)
        hlay.addStretch()
        hlay.addWidget(close_btn)
        lay.addWidget(header)

        # --- What is being heard, right now ---
        self.heard_label = QLabel("Esperando audio...")
        self.heard_label.setObjectName("panelheard")
        self.heard_label.setWordWrap(True)
        self.heard_label.setMinimumHeight(38)
        lay.addWidget(self.heard_label)

        # --- The answer, read here without switching windows ---
        self.answer_box = QTextEdit()
        self.answer_box.setReadOnly(True)
        self.answer_box.setObjectName("panelanswer")
        self.answer_box.setFont(QFont("Segoe UI", 11))
        lay.addWidget(self.answer_box, stretch=1)

        self.translation_box = QTextEdit()
        self.translation_box.setReadOnly(True)
        self.translation_box.setObjectName("paneltranslation")
        self.translation_box.setFont(QFont("Segoe UI", 10))
        self.translation_box.setMaximumHeight(90)
        # Stays out of the way until there is a translation to show: an empty
        # tinted block in a small panel is pure noise.
        self.translation_box.hide()
        lay.addWidget(self.translation_box)

        # --- Actions ---
        actions = QHBoxLayout()
        actions.setSpacing(6)
        self.action_btn = QPushButton("■ Enviar y responder")
        self.action_btn.setObjectName("panelaction")
        self.action_btn.setFixedHeight(34)
        self.action_btn.setCursor(Qt.PointingHandCursor)
        self.action_btn.clicked.connect(self._on_action)
        self.es_btn = QPushButton("👁️ ES")
        self.es_btn.setObjectName("panelghost")
        self.es_btn.setFixedHeight(34)
        self.es_btn.setCursor(Qt.PointingHandCursor)
        self.es_btn.setToolTip("Mostrar/ocultar la traducción")
        self.es_btn.clicked.connect(self._toggle_es)
        actions.addWidget(self.action_btn, stretch=1)
        actions.addWidget(self.es_btn)
        lay.addLayout(actions)

        # Corner handle, kept alongside the full-border grips added below.
        grip_row = QHBoxLayout()
        grip_row.setContentsMargins(0, 0, 0, 0)
        grip_row.addStretch()
        grip_row.addWidget(QSizeGrip(self), 0, Qt.AlignBottom | Qt.AlignRight)
        lay.addLayout(grip_row)

        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setStyleSheet(theme.panel_stylesheet())

        # Every edge/corner resizes, not just the QSizeGrip's bottom-right one.
        self._reposition_grips = _add_resize_grips(self)

        geo = _read_geometry(PANEL_GEOMETRY_FILE, PANEL_DEFAULT)
        self.setGeometry(*geo)
        self._mode = "listening"  # listening -> processing -> done
        self._es_visible = True

    # --- Actions ---
    def _on_action(self) -> None:
        if self._mode == "listening":
            self.send_requested.emit()
        elif self._mode == "processing":
            self.cancel_requested.emit()
        elif self._mode == "done":
            self.listen_requested.emit()

    def _toggle_es(self) -> None:
        self._es_visible = not self._es_visible
        self._sync_translation()
        self.es_btn.setText("👁️ ES" if self._es_visible else "👁️‍🗨️ ES")

    def _sync_translation(self) -> None:
        has_text = bool(self.translation_box.toPlainText().strip())
        self.translation_box.setVisible(self._es_visible and has_text)

    def set_translation(self, text: str) -> None:
        self.translation_box.setPlainText(text)
        self._sync_translation()

    # --- State ---
    def set_listening(self) -> None:
        self._mode = "listening"
        self.action_btn.setEnabled(True)
        self.action_btn.setText("■ Enviar y responder")
        self.answer_box.clear()
        self.translation_box.clear()
        self._sync_translation()

    def set_processing(self, seconds: int) -> None:
        # Stays enabled on purpose: a disabled button here is what turned a slow
        # answer into a dead app with no way out but restarting.
        self._mode = "processing"
        self.action_btn.setEnabled(True)
        self.action_btn.setText(f"✕ Cancelar envío · {seconds}s")
        self.action_btn.setToolTip("Descartar este envío y volver a escuchar")

    def set_done(self) -> None:
        self._mode = "done"
        self.action_btn.setEnabled(True)
        self.action_btn.setText("● Escuchar de nuevo")

    def showEvent(self, event) -> None:
        super().showEvent(event)
        glass = theme.apply_glass(self, small_corners=True)
        self.setStyleSheet(theme.panel_stylesheet(own_corners=not glass))
        _apply_stealth(self)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        reposition = getattr(self, "_reposition_grips", None)
        if reposition is not None:
            reposition()

    def closeEvent(self, event) -> None:
        _write_geometry(PANEL_GEOMETRY_FILE, self)
        super().closeEvent(event)


class PromptEditor(QDialog):
    """Full-size editor for the meeting prompt.

    The composer strip is sized for a glance, not for rewriting a long briefing.
    This gives the prompt a real editing surface — big, resizable, with the line
    and word count visible — so parts can be reworked or cut without squinting
    through a four-line window.
    """

    def __init__(self, parent: QWidget, text: str) -> None:
        super().__init__(parent)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Dialog)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setModal(True)
        self.setMinimumSize(560, 400)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        shell = QWidget()
        shell.setObjectName("shell")
        outer.addWidget(shell)

        lay = QVBoxLayout(shell)
        lay.setContentsMargins(
            theme.SPACE_LG, theme.SPACE_SM, theme.SPACE_LG, theme.SPACE_LG
        )
        lay.setSpacing(theme.SPACE_MD)

        header = DragBar(self)
        header.setFixedHeight(38)
        hlay = QHBoxLayout(header)
        hlay.setContentsMargins(0, 0, 0, 0)
        title = QLabel("Contexto de la reunión")
        title.setObjectName("brand")
        hlay.addWidget(title)
        hlay.addStretch()
        close_btn = QPushButton("✕")
        close_btn.setObjectName("closebtn")
        close_btn.setFixedSize(32, 28)
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.clicked.connect(self.reject)
        hlay.addWidget(close_btn)
        lay.addWidget(header)

        self.editor = QTextEdit()
        self.editor.setObjectName("contextbox")
        self.editor.setFont(QFont("Segoe UI Variable Text", theme.PT_BODY))
        self.editor.setPlainText(text)
        self.editor.textChanged.connect(self._update_count)
        lay.addWidget(self.editor, stretch=1)

        footer = QHBoxLayout()
        footer.setSpacing(theme.SPACE_SM)
        self.count_label = QLabel("")
        self.count_label.setObjectName("chiplabel")
        footer.addWidget(self.count_label)
        footer.addStretch()

        cancel_btn = QPushButton("Cancelar")
        cancel_btn.setObjectName("ghost")
        cancel_btn.setFixedHeight(36)
        cancel_btn.setCursor(Qt.PointingHandCursor)
        cancel_btn.clicked.connect(self.reject)

        save_btn = QPushButton("Guardar")
        save_btn.setObjectName("primary")
        save_btn.setFixedHeight(36)
        save_btn.setCursor(Qt.PointingHandCursor)
        save_btn.setDefault(True)
        save_btn.clicked.connect(self.accept)

        footer.addWidget(cancel_btn)
        footer.addWidget(save_btn)
        footer.addWidget(QSizeGrip(self), 0, Qt.AlignBottom | Qt.AlignRight)
        lay.addLayout(footer)

        self._update_count()
        geo = _read_geometry(EDITOR_GEOMETRY_FILE, (0, 0, 780, 560))
        self.resize(geo[2], geo[3])

    def _update_count(self) -> None:
        text = self.editor.toPlainText()
        words = len(text.split())
        lines = text.count("\n") + 1 if text else 0
        self.count_label.setText(f"{words} palabras · {lines} líneas")

    def text(self) -> str:
        return self.editor.toPlainText().strip()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        glass = theme.apply_glass(self)
        self.setStyleSheet(theme.stylesheet(own_corners=not glass))
        _apply_stealth(self)
        self.editor.setFocus()

    def closeEvent(self, event) -> None:
        _write_geometry(EDITOR_GEOMETRY_FILE, self)
        super().closeEvent(event)


class SettingsPopover(QWidget):
    """Small popover for app-wide settings, opened from the ⚙ button.

    A flat list of rows so more settings can be dropped in later without new
    scaffolding: each row is just a label, its tooltip and a ToggleSwitch.
    """

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent, Qt.Popup)
        self.setObjectName("settingspopover")
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFixedWidth(280)
        self.setStyleSheet(
            theme.stylesheet()
            + f"""
            #settingspopover {{
                background-color: {theme.GLASS_BASE};
                border: 1px solid {theme.STROKE};
                border-radius: {theme.RADIUS_MD}px;
            }}
            """
        )

        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(10)

        header = QHBoxLayout()
        title = QLabel("Configuración")
        title.setObjectName("sectionlabel")
        header.addWidget(title)
        header.addStretch()
        close_btn = QPushButton("✕")
        close_btn.setObjectName("closebtn")
        close_btn.setFixedSize(20, 20)
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.clicked.connect(self.hide)
        header.addWidget(close_btn)
        lay.addLayout(header)

        self._rows = QVBoxLayout()
        self._rows.setSpacing(10)
        lay.addLayout(self._rows)

    def add_toggle_row(self, label_text: str, tooltip: str, checked: bool) -> ToggleSwitch:
        row = QHBoxLayout()
        row.setSpacing(10)
        label = QLabel(label_text)
        label.setObjectName("chiplabel")
        label.setWordWrap(True)
        label.setToolTip(tooltip)
        switch = ToggleSwitch()
        switch.setChecked(checked)
        switch.setToolTip(tooltip)
        row.addWidget(label, stretch=1)
        row.addWidget(switch)
        self._rows.addLayout(row)
        return switch


class ModelLoader(QThread):
    """Constructs Whisper, Gemini and Argos off the GUI thread.

    Building these synchronously in main() is what used to stall the window
    from appearing at all. Same idiom as CopilotWorker: no return value, just
    signals, so the window never touches the models until they exist.
    """

    status = Signal(str)
    ready = Signal(object, object, object)  # transcriber, brain, translator
    failed = Signal(str)

    def run(self) -> None:
        try:
            self.status.emit("Cargando modelo de voz (puede tardar unos segundos)...")
            transcriber = Transcriber()
            self.status.emit("Conectando con Gemini...")
            brain = Brain()
            self.status.emit("Preparando traductor offline...")
            translator = Translator()
        except Exception as exc:  # noqa: BLE001 - surfaced in the UI, not a hidden console
            self.failed.emit(str(exc))
            return
        self.ready.emit(transcriber, brain, translator)


class ChatWindow(QWidget):
    """Setup and full transcript. The Live panel takes over during the call."""

    def __init__(self) -> None:
        super().__init__()
        # Whisper/Gemini/Argos load in the background (see _start_model_loading)
        # so the window can show right away; every call site that needs them
        # must check _models_loaded (or that brain/etc. isn't None) first.
        self.transcriber: Transcriber | None = None
        self.brain: Brain | None = None
        self.translator: Translator | None = None
        self._models_loaded = False
        self.usage = UsageTracker(USAGE_FILE)  # a path + a tiny text file, cheap enough
        self.worker: CopilotWorker | None = None
        # The conversation being recorded right now. Each one is independent: a
        # new chat never inherits the previous chat's memory.
        self.convo = conversations.new_conversation()
        # Id of a saved conversation being read back, or None when the active one
        # is on screen. Reading an old call never revives it.
        self._viewing: str | None = None

        self._answer_length = self._load_length()  # applied to `brain` once it exists
        self._processing_secs = 0
        self._processing_timer = QTimer(self)
        self._processing_timer.timeout.connect(self._tick_processing)

        # True between pressing send and the answer arriving; while it holds, the
        # action button cancels instead of sending.
        self._processing = False
        self._send_cancelled = False
        # Last live transcription seen. Kept so cancelling gives it back instead
        # of throwing away everything the app just heard.
        self._last_heard = ""

        self.panel = LivePanel()
        self.panel.send_requested.connect(self._send_now)
        self.panel.listen_requested.connect(self._start_listening)
        self.panel.cancel_requested.connect(self._cancel_send)
        self.panel.closed.connect(self._close_panel)

        _apply_app_icon(self)
        self._build_ui()
        # Every edge/corner resizes, not just the QSizeGrip's bottom-right one.
        self._reposition_grips = _add_resize_grips(self)
        self._start_model_loading()

    # ------------------------------------------------------------- startup
    def _start_model_loading(self) -> None:
        """Kick off the one-and-only background load of Whisper/Gemini/Argos."""
        if getattr(self, "_model_loader", None) is not None:
            return  # already loading (or loaded) — never start a second one
        self._model_loader = ModelLoader()
        self._model_loader.status.connect(self.status_label.setText)
        self._model_loader.ready.connect(self._on_models_ready)
        self._model_loader.failed.connect(self._on_models_failed)
        self._model_loader.start()

    def _on_models_ready(self, transcriber, brain, translator) -> None:
        self.transcriber = transcriber
        self.brain = brain
        self.translator = translator
        self._models_loaded = True
        self.brain.set_length(self._answer_length)
        self.status_label.setText("Listo")
        self.listen_btn.setEnabled(True)

    def _on_models_failed(self, message: str) -> None:
        self.status_label.setText("⚠️ Error al cargar los modelos")
        QMessageBox.critical(
            self,
            "Error al cargar",
            "No se pudieron cargar los modelos de voz/IA:\n\n"
            f"{message}\n\n"
            "Revisa el README (sección de solución de problemas) si falta "
            "CUDA o la GEMINI_API_KEY.",
        )

    # ---------------------------------------------------------------- UI
    def _build_ui(self) -> None:
        self.setWindowTitle("CallAssist")
        self.setMinimumSize(900, 580)
        # Frameless so the acrylic backdrop and the rounded shell are ours to
        # define; Windows still supplies the drop shadow and corner rounding.
        self.setWindowFlags(Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        shell = QWidget()
        shell.setObjectName("shell")
        outer.addWidget(shell)

        shell_lay = QVBoxLayout(shell)
        shell_lay.setContentsMargins(0, 0, 0, 0)
        shell_lay.setSpacing(0)
        shell_lay.addWidget(self._build_header())

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        body.addWidget(self._build_sidebar())
        body.addWidget(self._build_main(), stretch=1)
        shell_lay.addLayout(body, stretch=1)

        self.setStyleSheet(theme.stylesheet())
        QShortcut(QKeySequence("Ctrl+E"), self, self._open_prompt_editor)
        self._refresh_recents()  # past calls are there the moment the app opens
        geo = _read_geometry(CHAT_GEOMETRY_FILE)
        if geo:
            self.setGeometry(*geo)

    def _build_sidebar(self) -> QWidget:
        side = QWidget()
        side.setObjectName("sidebar")
        side.setFixedWidth(252)
        lay = QVBoxLayout(side)
        lay.setContentsMargins(
            theme.SPACE_MD, theme.SPACE_MD, theme.SPACE_MD, theme.SPACE_MD
        )
        lay.setSpacing(theme.SPACE_MD)

        new_btn = QPushButton("＋   Nueva conversación")
        new_btn.setObjectName("newchat")
        new_btn.setFixedHeight(38)
        new_btn.setCursor(Qt.PointingHandCursor)
        new_btn.clicked.connect(self._new_conversation)
        lay.addWidget(new_btn)

        self.search = QLineEdit()
        self.search.setObjectName("search")
        self.search.setPlaceholderText("🔎 Buscar en tus conversaciones")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._refresh_recents)
        lay.addWidget(self.search)

        # Saved calls on top, questions inside the open call below. A splitter so
        # whichever one is being used can take the space.
        split = QSplitter(Qt.Vertical)
        split.setObjectName("sidesplit")
        split.setHandleWidth(6)

        recents_box = QWidget()
        recents_lay = QVBoxLayout(recents_box)
        recents_lay.setContentsMargins(0, 0, 0, 0)
        recents_lay.setSpacing(4)
        recents_label = QLabel("Conversaciones guardadas")
        recents_label.setObjectName("sectionlabel")
        recents_lay.addWidget(recents_label)
        self.recents_list = self._make_list()
        self.recents_list.itemClicked.connect(self._on_recent_clicked)
        self.recents_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.recents_list.customContextMenuRequested.connect(self._on_recent_menu)
        self.recents_list.setToolTip("Clic para abrir. Clic derecho para borrar.")
        recents_lay.addWidget(self.recents_list, stretch=1)
        split.addWidget(recents_box)

        questions_box = QWidget()
        questions_lay = QVBoxLayout(questions_box)
        questions_lay.setContentsMargins(0, 0, 0, 0)
        questions_lay.setSpacing(4)
        questions_label = QLabel("Preguntas de esta conversación")
        questions_label.setObjectName("sectionlabel")
        questions_lay.addWidget(questions_label)
        self.history_list = self._make_list()
        self.history_list.itemClicked.connect(self._on_history_clicked)
        questions_lay.addWidget(self.history_list, stretch=1)
        split.addWidget(questions_box)

        split.setSizes([260, 200])
        lay.addWidget(split, stretch=1)

        self.usage_label = QLabel("")
        self.usage_label.setObjectName("usage")
        self.usage_label.setToolTip(
            "Estimado de pedidos REALES de hoy. No es el número oficial de Google."
        )
        lay.addWidget(self.usage_label)
        self._update_usage_label(self.usage.today_count())
        return side

    def _build_header(self) -> QWidget:
        """Own title bar: brand on the left, window controls on the right."""
        header = DragBar(self)
        header.setFixedHeight(46)
        lay = QHBoxLayout(header)
        lay.setContentsMargins(theme.SPACE_LG, 0, theme.SPACE_SM, 0)
        lay.setSpacing(theme.SPACE_SM)

        brand_row = QHBoxLayout()
        brand_row.setSpacing(8)
        brand_icon = QLabel()
        app = QApplication.instance()
        app_icon = app.windowIcon() if app is not None else QIcon()
        if not app_icon.isNull():
            # Real icon once assets/icon.ico exists; the glyph is only a
            # fallback for a checkout that hasn't added it yet.
            brand_icon.setPixmap(app_icon.pixmap(18, 18))
        else:
            brand_icon.setText("◈")
        brand_row.addWidget(brand_icon)
        brand = QLabel("CallAssist")
        brand.setObjectName("brand")
        brand_row.addWidget(brand)
        lay.addLayout(brand_row)
        lay.addStretch()

        # Overwritten almost immediately by ModelLoader's own status signal;
        # this is just what's on screen for the instant before that arrives.
        self.status_label = QLabel("Cargando modelo de voz…")
        self.status_label.setObjectName("status")
        lay.addWidget(self.status_label)

        self._build_settings_popover()
        self.gear_btn = QPushButton("⚙")
        self.gear_btn.setObjectName("windowbtn")
        self.gear_btn.setFixedSize(32, 28)
        self.gear_btn.setCursor(Qt.PointingHandCursor)
        self.gear_btn.setToolTip("Configuración")
        self.gear_btn.clicked.connect(self._open_settings)
        lay.addWidget(self.gear_btn)

        for glyph, tip, slot, name in (
            ("─", "Minimizar", self.showMinimized, "windowbtn"),
            ("✕", "Cerrar", self.close, "closebtn"),
        ):
            button = QPushButton(glyph)
            button.setObjectName(name)
            button.setFixedSize(32, 28)
            button.setCursor(Qt.PointingHandCursor)
            button.setToolTip(tip)
            button.clicked.connect(slot)
            lay.addWidget(button)
        return header

    def _build_settings_popover(self) -> None:
        """App-wide settings, reached from the ⚙ button (see _open_settings)."""
        self.settings_popover = SettingsPopover(self)
        stealth_tip = (
            "Las ventanas de la app no se verán cuando compartas pantalla "
            "(tú sí las ves)."
        )
        self.stealth_switch = self.settings_popover.add_toggle_row(
            "Ocultar al compartir pantalla", stealth_tip, _stealth_enabled
        )
        self.stealth_switch.toggled.connect(self._on_stealth_toggled)

    def _open_settings(self) -> None:
        pos = self.gear_btn.mapToGlobal(self.gear_btn.rect().bottomRight())
        self.settings_popover.adjustSize()
        self.settings_popover.move(
            pos.x() - self.settings_popover.width(), pos.y() + 6
        )
        self.settings_popover.show()

    def _build_main(self) -> QWidget:
        main = QWidget()
        main.setObjectName("mainarea")
        lay = QVBoxLayout(main)
        # Margins here are what let the composer read as a card floating over the
        # transcript rather than a bar welded to the bottom edge.
        lay.setContentsMargins(theme.SPACE_LG, 0, theme.SPACE_LG, theme.SPACE_LG)
        lay.setSpacing(theme.SPACE_MD)

        # Transcript and composer share a draggable split, so the prompt area can
        # be pulled as tall as the briefing needs instead of living in four lines.
        self.body_split = QSplitter(Qt.Vertical)
        self.body_split.setObjectName("bodysplit")
        self.body_split.setHandleWidth(10)
        self.body_split.setChildrenCollapsible(False)

        self.chat = ChatView()
        self.body_split.addWidget(self.chat)

        # --- Composer: the whole call is configured here, before it starts ---
        composer = QWidget()
        composer.setObjectName("composer")
        composer.setMinimumHeight(180)
        clay = QVBoxLayout(composer)
        clay.setContentsMargins(16, 12, 16, 14)
        clay.setSpacing(theme.SPACE_SM)

        # --- Prompt header: says what this is, and offers a way out to a real
        # editing surface when the briefing is longer than a glance ---
        ctx_row = QHBoxLayout()
        ctx_row.setSpacing(theme.SPACE_SM)
        ctx_label = QLabel("Contexto de la reunión")
        ctx_label.setObjectName("sectionlabel")
        expand_btn = QPushButton("⤢  Ampliar")
        expand_btn.setObjectName("ghost")
        expand_btn.setFixedHeight(26)
        expand_btn.setCursor(Qt.PointingHandCursor)
        expand_btn.setToolTip(
            "Abrir el prompt en una ventana grande para editarlo cómodo (Ctrl+E)"
        )
        expand_btn.clicked.connect(self._open_prompt_editor)
        ctx_row.addWidget(ctx_label)
        ctx_row.addStretch()
        ctx_row.addWidget(expand_btn)
        clay.addLayout(ctx_row)

        self.context_box = QTextEdit()
        self.context_box.setObjectName("contextbox")
        self.context_box.setPlaceholderText(
            "Contexto de la reunión: de qué se habla y cómo quieres responder. "
            "Ej: Job interview for a backend role. 3 years with Python. "
            "Answer confidently, concise and professional."
        )
        self.context_box.setFont(QFont("Segoe UI Variable Text", theme.PT_SMALL))
        # No fixed height: it takes whatever the split gives it, so dragging the
        # divider up is what makes the prompt readable.
        self.context_box.setMinimumHeight(64)
        self.context_box.setPlainText(_read_text(CONTEXT_FILE))
        clay.addWidget(self.context_box, stretch=1)

        # Chips row: the few knobs that change per call, inline like a model picker.
        chips = QHBoxLayout()
        chips.setSpacing(8)

        self.device_combo = QComboBox()
        self.device_combo.setObjectName("chip")
        self.device_combo.setToolTip("Qué salida de audio escuchar")
        self._populate_devices()

        self.mode_combo = QComboBox()
        self.mode_combo.setObjectName("chip")
        self.mode_combo.addItem("✋ Controlado", "controlled")
        self.mode_combo.addItem("🤖 Automático", "auto")
        self.mode_combo.setToolTip(
            "Controlado: junta todo y responde cuando pulsas Enviar.\n"
            "Automático: responde en cada pausa."
        )
        index = self.mode_combo.findData(_read_text(MODE_FILE, "controlled").strip())
        self.mode_combo.setCurrentIndex(index if index >= 0 else 0)
        self.mode_combo.currentIndexChanged.connect(
            lambda: _write_text(MODE_FILE, self._selected_mode())
        )

        length_label = QLabel("✂️")
        length_label.setToolTip("Respuestas cortas / detalladas")
        self.length_switch = ToggleSwitch()
        self.length_switch.setChecked(self._answer_length == "detailed")
        self.length_switch.setToolTip(
            "Apagado: respuestas cortas (1-2 frases).\nEncendido: detalladas."
        )
        self.length_switch.toggled.connect(self._on_length_toggled)
        self.length_hint = QLabel("Cortas")
        self.length_hint.setObjectName("chiplabel")

        chips.addWidget(self.device_combo, stretch=2)
        chips.addWidget(self.mode_combo, stretch=1)
        chips.addStretch()
        chips.addWidget(length_label)
        chips.addWidget(self.length_switch)
        chips.addWidget(self.length_hint)
        clay.addLayout(chips)

        # --- Primary action ---
        action_row = QHBoxLayout()
        action_row.setSpacing(8)
        self.listen_btn = QPushButton("●  Escuchar la llamada")
        self.listen_btn.setObjectName("primary")
        self.listen_btn.setFixedHeight(42)
        self.listen_btn.setCursor(Qt.PointingHandCursor)
        self.listen_btn.setEnabled(False)  # enabled once _on_models_ready fires
        self.listen_btn.clicked.connect(self._toggle)
        action_row.addWidget(self.listen_btn, stretch=1)
        # Corner handle, tucked beside the main action; the full-border grips
        # added in __init__ cover the rest of the window's edges.
        action_row.addWidget(QSizeGrip(self), 0, Qt.AlignBottom | Qt.AlignRight)
        clay.addLayout(action_row)

        theme.elevate(composer, blur=30, alpha=120, dy=4)
        self.body_split.addWidget(composer)
        self.body_split.setStretchFactor(0, 1)  # transcript absorbs spare space
        self.body_split.setStretchFactor(1, 0)
        self.body_split.setSizes(self._load_split())
        self.body_split.splitterMoved.connect(lambda *_: self._save_split())
        lay.addWidget(self.body_split, stretch=1)
        self._update_length_hint()
        return main

    # --- Prompt editing -----------------------------------------------------
    def _open_prompt_editor(self) -> None:
        """Edit the prompt in a window that fits it."""
        dialog = PromptEditor(self, self.context_box.toPlainText())
        if dialog.exec() == QDialog.Accepted:
            text = dialog.text()
            self.context_box.setPlainText(text)
            _write_text(CONTEXT_FILE, text)
            # A running call picks up the edit on its next question. No-op while
            # the models are still loading — _start_listening reads the box
            # fresh anyway once they're ready, so nothing is lost.
            if self.brain is not None:
                self.brain.set_context(text)

    def _load_split(self) -> list[int]:
        try:
            top, bottom = (int(v) for v in _read_text(SPLIT_FILE).split(","))
            return [max(top, 80), max(bottom, 180)]
        except ValueError:
            return [420, 220]

    def _save_split(self) -> None:
        sizes = self.body_split.sizes()
        if len(sizes) == 2:
            _write_text(SPLIT_FILE, f"{sizes[0]},{sizes[1]}")

    def _make_list(self) -> QListWidget:
        """Sidebar list styled to elide long text instead of scrolling sideways."""
        widget = QListWidget()
        widget.setObjectName("historylist")
        widget.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        widget.setTextElideMode(Qt.ElideRight)
        widget.setWordWrap(False)
        return widget

    # ------------------------------------------------------------ settings
    def _selected_mode(self) -> str:
        return self.mode_combo.currentData() or "controlled"

    def _selected_device(self) -> dict | None:
        return self.device_combo.currentData()

    def _populate_devices(self) -> None:
        self.device_combo.clear()
        try:
            devices = list_loopback_devices()
        except Exception as exc:  # noqa: BLE001
            self.device_combo.addItem(f"(sin dispositivos: {exc})", None)
            return
        if not devices:
            self.device_combo.addItem("(no se detectaron dispositivos)", None)
            return
        saved = _read_text(DEVICE_FILE).strip()
        selected = 0
        for i, dev in enumerate(devices):
            self.device_combo.addItem(
                "🔊 " + dev["name"] + (" — predeterminado" if dev["is_default"] else ""),
                dev,
            )
            if saved and dev["name"] == saved:
                selected = i
            elif not saved and dev["is_default"]:
                selected = i
        self.device_combo.setCurrentIndex(selected)

    def _load_length(self) -> str:
        value = _read_text(LENGTH_FILE).strip()
        return value if value in ("short", "detailed") else "short"

    def _on_length_toggled(self, detailed: bool) -> None:
        self._answer_length = "detailed" if detailed else "short"
        # No-op while loading; _on_models_ready applies self._answer_length
        # once `brain` exists.
        if self.brain is not None:
            self.brain.set_length(self._answer_length)
        _write_text(LENGTH_FILE, self._answer_length)
        self._update_length_hint()

    def _update_length_hint(self) -> None:
        self.length_hint.setText(
            "Detalladas" if self._answer_length == "detailed" else "Cortas"
        )

    def _on_stealth_toggled(self, hidden: bool) -> None:
        # Module-level: shared with LivePanel's own showEvent, so a panel
        # shown later (or the panel already on screen) reflects it too.
        global _stealth_enabled
        _stealth_enabled = hidden
        _write_text(HIDE_FILE, "1" if hidden else "0")
        _apply_stealth(self)
        _apply_stealth(self.panel)

    # ------------------------------------------------------------- control
    def _toggle(self) -> None:
        if self._processing:
            self._cancel_send()
        elif self.worker and self.worker.isRunning():
            if self._selected_mode() == "controlled":
                self._send_now()
            else:
                self._stop_listening()
        else:
            self._start_listening()

    def _start_listening(self) -> None:
        if not self._models_loaded:
            return  # Whisper/Gemini/Argos not ready yet; listen_btn is disabled too
        if self.worker and self.worker.isRunning():
            return
        # Listening while reading an old call opens a new one instead of
        # appending to it: saved conversations are a record, not a session.
        if self._viewing is not None:
            self._new_conversation()

        context = self.context_box.toPlainText().strip()
        _write_text(CONTEXT_FILE, context)

        device = self._selected_device()
        if device:
            _write_text(DEVICE_FILE, device["name"])
        mode = self._selected_mode()

        listener = Listener(
            self.transcriber, device_index=device["index"] if device else None
        )
        self.worker = CopilotWorker(
            listener, self.brain, self.translator, self.usage, context, mode
        )
        self.worker.question_detected.connect(self._on_question)
        self.worker.partial_text.connect(self._on_partial)
        self.worker.answer_chunk.connect(self._on_chunk)
        self.worker.translation_ready.connect(self._on_translation)
        self.worker.exchange_recorded.connect(self._on_exchange_recorded)
        self.worker.hearing.connect(self._on_hearing)
        self.worker.usage_updated.connect(self._update_usage_label)
        self.worker.status.connect(self.status_label.setText)
        self.worker.finished.connect(self._on_worker_finished)
        self.worker.start()

        self._last_heard = ""  # a new capture starts from nothing heard
        self._processing = False
        self._send_cancelled = False
        self.mode_combo.setEnabled(False)
        self.listen_btn.setText(
            "■  Enviar y responder" if mode == "controlled" else "■  Detener"
        )
        self.panel.set_listening()
        self.panel.show()
        self.panel.raise_()

    def _send_now(self) -> None:
        """Controlled mode: stop capturing and let the answer stream in."""
        if not (self.worker and self.worker.isRunning()):
            return
        self.worker.stop()
        self._processing = True
        self._send_cancelled = False
        self._processing_secs = 0
        self.panel.set_processing(0)
        self.listen_btn.setText("✕  Cancelar envío · 0s")
        self.status_label.setText("Procesando lo escuchado...")
        self._processing_timer.start(1000)

    def _cancel_send(self) -> None:
        """Undo an accidental send: drop the request, keep what was heard.

        The transcription is the expensive part — it is what the other person
        actually said — so cancelling returns it rather than discarding it, and
        the next capture starts from a clean slate.
        """
        if not self._processing:
            return
        self._send_cancelled = True
        self._processing_timer.stop()
        if self.worker:
            self.worker.cancel()
        self.status_label.setText("Envío cancelado")
        self.panel.state_label.setText("✕ Envío cancelado")
        heard = self._last_heard.strip()
        if heard:
            self.panel.heard_label.setText(f"👂 {heard}")
            self.chat.add_system(f"✕ Envío cancelado. Se había escuchado: “{heard}”")
        else:
            self.panel.heard_label.setText("Envío cancelado. Nada que conservar.")

    def _tick_processing(self) -> None:
        self._processing_secs += 1
        self.panel.set_processing(self._processing_secs)
        self.listen_btn.setText(f"✕  Cancelar envío · {self._processing_secs}s")

    def _stop_listening(self) -> None:
        if self.worker:
            self.worker.stop()
            self.worker.wait(3000)

    def _on_worker_finished(self) -> None:
        self._processing_timer.stop()
        self._processing = False
        self.mode_combo.setEnabled(True)
        self.listen_btn.setEnabled(True)
        self.listen_btn.setText("●  Escuchar la llamada")
        self.panel.set_done()
        if self._send_cancelled:
            self._send_cancelled = False
            self.status_label.setText("Envío cancelado")
            self.panel.state_label.setText("✕ Envío cancelado")
        else:
            self.status_label.setText("Listo")
            self.panel.state_label.setText("✅ Respuesta lista")

    def _close_panel(self) -> None:
        self.panel.hide()
        if self.worker and self.worker.isRunning():
            self._stop_listening()

    # -------------------------------------------------------------- signals
    def _on_question(self, text: str) -> None:
        self._last_heard = text
        self.chat.add_question(text)
        self.panel.heard_label.setText(f"👂 {text}")
        self.panel.answer_box.clear()
        self.panel.set_translation("")

    def _on_partial(self, text: str) -> None:
        self._last_heard = text
        self.panel.heard_label.setText(f"👂 {text}")

    def _on_chunk(self, text: str) -> None:
        self.chat.append_answer(text)
        cursor = self.panel.answer_box.textCursor()
        cursor.movePosition(QTextCursor.End)
        self.panel.answer_box.setTextCursor(cursor)
        self.panel.answer_box.insertPlainText(text)
        self.panel.answer_box.ensureCursorVisible()

    def _on_translation(self, text: str) -> None:
        self.chat.set_translation(text)
        self.panel.set_translation(text)

    def _on_hearing(self, state: str) -> None:
        messages = {
            "idle": "🎧 Escuchando la llamada...",
            "speech": "🎤 La otra persona está hablando...",
            "capturing": "🔴 Grabando... pulsa «Enviar» cuando termine la pregunta.",
            "transcribing": "🧠 Entendiendo lo que dijo...",
        }
        self.panel.state_label.setText(messages.get(state, "🎧 En vivo"))

    def _update_usage_label(self, count: int) -> None:
        self.usage_label.setText(f"📨 ~{count} pedidos hoy (est.)")

    # -------------------------------------------------------------- history
    @property
    def _shown(self) -> list[dict]:
        """Exchanges currently on screen: a saved call, or the active one."""
        if self._viewing is not None:
            saved = conversations.load(self._viewing)
            return saved.exchanges if saved else []
        return self.convo.exchanges

    def _on_exchange_recorded(self, question: str, answer: str, translation: str) -> None:
        self.convo.exchanges.append(
            {"question": question, "answer": answer, "translation": translation}
        )
        # Saved after every exchange, not on exit: a crash mid-call must not
        # cost the transcript.
        conversations.save(self.convo)
        self._refresh_history_list()
        self._refresh_recents()

    def _refresh_history_list(self) -> None:
        self.history_list.clear()
        for i, entry in enumerate(self._shown):
            question = entry.get("question", "")
            item = QListWidgetItem(f"{i + 1}. {question}")
            item.setToolTip(question)
            item.setData(Qt.UserRole, i)
            self.history_list.addItem(item)

    def _on_history_clicked(self, item: QListWidgetItem) -> None:
        self.chat.scroll_to_exchange(item.data(Qt.UserRole))

    def _refresh_recents(self) -> None:
        """Rebuild the saved-calls list, filtered by the sidebar search."""
        needle = self.search.text().strip()
        self.recents_list.clear()
        for conversation in conversations.list_all():
            if not conversation.matches(needle):
                continue
            item = QListWidgetItem(f"{conversation.title}\n{conversation.when}")
            item.setToolTip(
                f"{conversation.title}\n{len(conversation.exchanges)} pregunta(s) · "
                f"{conversation.when}\n\nClic derecho para renombrar o borrar."
            )
            item.setData(Qt.UserRole, conversation.id)
            self.recents_list.addItem(item)

    def _on_recent_clicked(self, item: QListWidgetItem) -> None:
        """Open a saved call read-only. The AI is NOT given its memory back —
        every chat stays independent; this is a record, not a resumption."""
        conv_id = item.data(Qt.UserRole)
        saved = conversations.load(conv_id)
        if saved is None:
            return
        self._viewing = conv_id
        self._render(saved.exchanges)
        self.chat.add_system(
            "📁 Conversación guardada. Al pulsar «Escuchar» se abre una nueva."
        )
        self._refresh_history_list()
        self.listen_btn.setText("●  Escuchar (nueva conversación)")

    def _on_recent_menu(self, point) -> None:
        item = self.recents_list.itemAt(point)
        if item is None:
            return
        menu = QMenu(self)
        rename_action = menu.addAction("✏️  Poner título")
        clear_action = menu.addAction("↩️  Quitar el título")
        menu.addSeparator()
        delete_action = menu.addAction("🗑️  Borrar esta conversación")

        chosen = menu.exec(self.recents_list.mapToGlobal(point))
        conv_id = item.data(Qt.UserRole)
        if chosen is rename_action:
            self._rename_conversation(conv_id)
        elif chosen is clear_action:
            self._set_title(conv_id, "")
        elif chosen is delete_action:
            self._delete_conversation(conv_id)

    def _rename_conversation(self, conv_id: str) -> None:
        saved = conversations.load(conv_id)
        if saved is None:
            return
        title, accepted = QInputDialog.getText(
            self,
            "Título de la conversación",
            "Ponle un nombre que reconozcas después:",
            text=saved.custom_title or saved.title,
        )
        if accepted:
            self._set_title(conv_id, title)

    def _set_title(self, conv_id: str, title: str) -> None:
        saved = conversations.load(conv_id)
        if saved is None:
            return
        saved.custom_title = title.strip()
        conversations.save(saved)
        # The active conversation is held in memory and rewritten after every
        # exchange, so its copy has to learn the new title or the next save
        # would quietly undo the rename.
        if self.convo.id == conv_id:
            self.convo.custom_title = saved.custom_title
        self._refresh_recents()

    def _delete_conversation(self, conv_id: str) -> None:
        saved = conversations.load(conv_id)
        if saved is None:
            return
        # Deleting a transcript cannot be undone and the file is the only copy,
        # so this asks first — and says what is about to be lost.
        confirm = QMessageBox(self)
        confirm.setWindowTitle("Borrar conversación")
        confirm.setText(f"¿Borrar «{saved.title}»?")
        confirm.setInformativeText(
            f"{len(saved.exchanges)} pregunta(s) · {saved.when}\n"
            "Esto no se puede deshacer."
        )
        confirm.setIcon(QMessageBox.Warning)
        confirm.setStandardButtons(QMessageBox.Cancel | QMessageBox.Yes)
        confirm.setDefaultButton(QMessageBox.Cancel)
        confirm.button(QMessageBox.Yes).setText("Borrar")
        confirm.button(QMessageBox.Cancel).setText("Cancelar")
        if confirm.exec() != QMessageBox.Yes:
            return

        conversations.delete(conv_id)
        if self._viewing == conv_id:  # it was on screen: fall back to the active chat
            self._viewing = None
            self._render(self.convo.exchanges)
            self.listen_btn.setText("●  Escuchar la llamada")
        self._refresh_history_list()
        self._refresh_recents()

    def _render(self, exchanges: list[dict]) -> None:
        """Repaint the transcript from stored exchanges."""
        self.chat.clear()
        for entry in exchanges:
            self.chat.add_question(entry.get("question", ""))
            self.chat.append_answer(entry.get("answer", ""))
            translation = entry.get("translation", "")
            if translation:
                self.chat.set_translation(translation)
        self.panel.answer_box.clear()
        self.panel.set_translation("")

    def _new_conversation(self) -> None:
        """Start a fresh, independent chat. The previous one is already on disk."""
        context = self.context_box.toPlainText().strip()
        conversations.save(self.convo)  # no-op when it has no exchanges
        self.convo = conversations.new_conversation(context)
        self._viewing = None
        # No-op while loading — there is no memory to reset yet.
        if self.brain is not None:
            self.brain.reset(context)  # each chat starts with no memory, by design
        self.chat.clear()
        self._refresh_history_list()
        self._refresh_recents()
        self.listen_btn.setText("●  Escuchar la llamada")
        self.panel.answer_box.clear()
        self.panel.set_translation("")

    # --------------------------------------------------------------- window
    def showEvent(self, event) -> None:
        super().showEvent(event)
        # DWM rounds and clips the window itself when it takes the backdrop; we
        # only round in CSS when it did not, so the two never fight over corners.
        glass = theme.apply_glass(self)
        self.setStyleSheet(theme.stylesheet(own_corners=not glass))
        _apply_stealth(self)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        reposition = getattr(self, "_reposition_grips", None)
        if reposition is not None:
            reposition()

    def closeEvent(self, event) -> None:
        _write_geometry(CHAT_GEOMETRY_FILE, self)
        _write_geometry(PANEL_GEOMETRY_FILE, self.panel)
        conversations.save(self.convo)
        self._stop_listening()
        self.panel.close()
        event.accept()


def main() -> None:
    if sys.platform == "win32":
        # Must run before QApplication exists: it tells Windows this process
        # is its own app, so the taskbar uses our icon (set below) instead of
        # grouping it under the generic python.exe one.
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "FICF0X.CallAssist"
            )
        except Exception:  # noqa: BLE001 - taskbar grouping is best-effort
            pass

    app = QApplication(sys.argv)

    icon_path = ROOT / "assets" / "icon.ico"
    if not icon_path.exists():
        icon_path = ROOT / "assets" / "icon.png"
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))

    # Whisper/Gemini/Argos are heavy (GPU model load, network clients) and used
    # to be built here, blocking the window from appearing until all three were
    # ready. ChatWindow now shows immediately and loads them itself in the
    # background (see ModelLoader) — the status label carries what these prints
    # used to say, since pythonw has no console for them to go to anyway.
    window = ChatWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
