"""Chat-style redesign of the copilot (prototype).

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

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QFont, QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
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
from src.config import ROOT
from src.listener import Listener
from src.main import (
    CONTEXT_FILE,
    DEVICE_FILE,
    HIDE_FROM_SCREENSHARE,
    LENGTH_FILE,
    MODE_FILE,
    USAGE_FILE,
    WDA_EXCLUDEFROMCAPTURE,
    CopilotWorker,
    ToggleSwitch,
)
from src.transcriber import Transcriber
from src.translator import Translator
from src.usage import UsageTracker

# Geometry of each surface, remembered separately: they are moved and sized for
# different jobs (reading vs. glancing).
CHAT_GEOMETRY_FILE = ROOT / "chat_geometry.txt"
PANEL_GEOMETRY_FILE = ROOT / "panel_geometry.txt"

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


def _apply_stealth(widget: QWidget) -> None:
    """Hide the window from screen capture while keeping it visible locally."""
    if not HIDE_FROM_SCREENSHARE or sys.platform != "win32":
        return
    try:
        ctypes.windll.user32.SetWindowDisplayAffinity(
            int(widget.winId()), WDA_EXCLUDEFROMCAPTURE
        )
    except Exception:  # noqa: BLE001 - stealth is best-effort
        pass


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
    closed = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
        )
        self.setMinimumSize(320, 220)

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

        # Resize handle: cheaper and more predictable than hit-testing the edges.
        grip_row = QHBoxLayout()
        grip_row.setContentsMargins(0, 0, 0, 0)
        grip_row.addStretch()
        grip_row.addWidget(QSizeGrip(self), 0, Qt.AlignBottom | Qt.AlignRight)
        lay.addLayout(grip_row)

        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setStyleSheet(theme.panel_stylesheet())

        geo = _read_geometry(PANEL_GEOMETRY_FILE, PANEL_DEFAULT)
        self.setGeometry(*geo)
        self._mode = "listening"  # listening -> processing -> done
        self._es_visible = True

    # --- Actions ---
    def _on_action(self) -> None:
        if self._mode == "listening":
            self.send_requested.emit()
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
        self._mode = "processing"
        self.action_btn.setEnabled(False)
        self.action_btn.setText(f"⏳ Procesando... {seconds}s")

    def set_done(self) -> None:
        self._mode = "done"
        self.action_btn.setEnabled(True)
        self.action_btn.setText("● Escuchar de nuevo")

    def showEvent(self, event) -> None:
        super().showEvent(event)
        theme.apply_glass(self, small_corners=True)
        _apply_stealth(self)

    def closeEvent(self, event) -> None:
        _write_geometry(PANEL_GEOMETRY_FILE, self)
        super().closeEvent(event)


class ChatWindow(QWidget):
    """Setup and full transcript. The Live panel takes over during the call."""

    def __init__(
        self,
        transcriber: Transcriber,
        brain: Brain,
        translator: Translator,
        usage: UsageTracker,
    ) -> None:
        super().__init__()
        self.transcriber = transcriber
        self.brain = brain
        self.translator = translator
        self.usage = usage
        self.worker: CopilotWorker | None = None
        # The conversation being recorded right now. Each one is independent: a
        # new chat never inherits the previous chat's memory.
        self.convo = conversations.new_conversation()
        # Id of a saved conversation being read back, or None when the active one
        # is on screen. Reading an old call never revives it.
        self._viewing: str | None = None

        self._answer_length = self._load_length()
        self.brain.set_length(self._answer_length)
        self._processing_secs = 0
        self._processing_timer = QTimer(self)
        self._processing_timer.timeout.connect(self._tick_processing)

        self.panel = LivePanel()
        self.panel.send_requested.connect(self._send_now)
        self.panel.listen_requested.connect(self._start_listening)
        self.panel.closed.connect(self._close_panel)

        self._build_ui()

    # ---------------------------------------------------------------- UI
    def _build_ui(self) -> None:
        self.setWindowTitle("Copiloto")
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

        brand = QLabel("◈  Copiloto")
        brand.setObjectName("brand")
        lay.addWidget(brand)
        lay.addStretch()

        self.status_label = QLabel("Listo")
        self.status_label.setObjectName("status")
        lay.addWidget(self.status_label)

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

    def _build_main(self) -> QWidget:
        main = QWidget()
        main.setObjectName("mainarea")
        lay = QVBoxLayout(main)
        # Margins here are what let the composer read as a card floating over the
        # transcript rather than a bar welded to the bottom edge.
        lay.setContentsMargins(theme.SPACE_LG, 0, theme.SPACE_LG, theme.SPACE_LG)
        lay.setSpacing(theme.SPACE_MD)

        self.chat = ChatView()
        lay.addWidget(self.chat, stretch=1)

        # --- Composer: the whole call is configured here, before it starts ---
        composer = QWidget()
        composer.setObjectName("composer")
        clay = QVBoxLayout(composer)
        clay.setContentsMargins(16, 10, 16, 14)
        clay.setSpacing(8)

        self.context_box = QTextEdit()
        self.context_box.setObjectName("contextbox")
        self.context_box.setPlaceholderText(
            "Contexto de la reunión: de qué se habla y cómo quieres responder. "
            "Ej: Job interview for a backend role. 3 years with Python. "
            "Answer confidently, concise and professional."
        )
        self.context_box.setFont(QFont("Segoe UI", 10))
        self.context_box.setFixedHeight(74)
        self.context_box.setPlainText(_read_text(CONTEXT_FILE))
        clay.addWidget(self.context_box)

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
        self.listen_btn.clicked.connect(self._toggle)
        action_row.addWidget(self.listen_btn, stretch=1)
        # Resize handle for the frameless shell, tucked beside the main action.
        action_row.addWidget(QSizeGrip(self), 0, Qt.AlignBottom | Qt.AlignRight)
        clay.addLayout(action_row)

        theme.elevate(composer, blur=30, alpha=120, dy=4)
        lay.addWidget(composer)
        self._update_length_hint()
        return main

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
        self.brain.set_length(self._answer_length)
        _write_text(LENGTH_FILE, self._answer_length)
        self._update_length_hint()

    def _update_length_hint(self) -> None:
        self.length_hint.setText(
            "Detalladas" if self._answer_length == "detailed" else "Cortas"
        )

    # ------------------------------------------------------------- control
    def _toggle(self) -> None:
        if self.worker and self.worker.isRunning():
            if self._selected_mode() == "controlled":
                self._send_now()
            else:
                self._stop_listening()
        else:
            self._start_listening()

    def _start_listening(self) -> None:
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
        self._processing_secs = 0
        self.panel.set_processing(0)
        self.listen_btn.setEnabled(False)
        self.listen_btn.setText("⏳  Procesando... 0s")
        self.status_label.setText("Procesando lo escuchado...")
        self._processing_timer.start(1000)

    def _tick_processing(self) -> None:
        self._processing_secs += 1
        self.panel.set_processing(self._processing_secs)
        self.listen_btn.setText(f"⏳  Procesando... {self._processing_secs}s")

    def _stop_listening(self) -> None:
        if self.worker:
            self.worker.stop()
            self.worker.wait(3000)

    def _on_worker_finished(self) -> None:
        self._processing_timer.stop()
        self.mode_combo.setEnabled(True)
        self.listen_btn.setEnabled(True)
        self.listen_btn.setText("●  Escuchar la llamada")
        self.status_label.setText("Listo")
        self.panel.set_done()
        self.panel.state_label.setText("✅ Respuesta lista")

    def _close_panel(self) -> None:
        self.panel.hide()
        if self.worker and self.worker.isRunning():
            self._stop_listening()

    # -------------------------------------------------------------- signals
    def _on_question(self, text: str) -> None:
        self.chat.add_question(text)
        self.panel.heard_label.setText(f"👂 {text}")
        self.panel.answer_box.clear()
        self.panel.set_translation("")

    def _on_partial(self, text: str) -> None:
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
                f"{conversation.when}"
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
        delete_action = menu.addAction("🗑️ Borrar esta conversación")
        if menu.exec(self.recents_list.mapToGlobal(point)) is not delete_action:
            return
        conv_id = item.data(Qt.UserRole)
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
        theme.apply_glass(self)

    def closeEvent(self, event) -> None:
        _write_geometry(CHAT_GEOMETRY_FILE, self)
        _write_geometry(PANEL_GEOMETRY_FILE, self.panel)
        conversations.save(self.convo)
        self._stop_listening()
        self.panel.close()
        event.accept()


def main() -> None:
    app = QApplication(sys.argv)

    print("Cargando modelo de transcripcion (puede tardar unos segundos)...")
    transcriber = Transcriber()
    print(f"[OK] Whisper en: {transcriber.device}")
    brain = Brain()
    print("[OK] Gemini listo.")
    print("Preparando traductor offline...")
    translator = Translator()
    print(f"[OK] Traductor offline listo: {translator.ready}. Abriendo ventana...")

    usage = UsageTracker(USAGE_FILE)
    window = ChatWindow(transcriber, brain, translator, usage)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
