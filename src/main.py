"""Floating overlay copilot.

Click "Escuchar" to start listening to system audio. When the other person on the
call finishes a question (detected by a pause), it is transcribed and Gemini's
answer streams into the window in real time.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import html
import sys
import webbrowser

# Windows: hide the window from screen capture / screen share while keeping it
# visible to the local user. Requires Windows 10 2004+.
WDA_EXCLUDEFROMCAPTURE = 0x00000011

# Switch: True = invisible in screen-share/screenshots. False = normal window.
HIDE_FROM_SCREENSHARE = False

# Width of the invisible border used to grab and resize the frameless window.
RESIZE_MARGIN = 8

from PySide6.QtCore import QPoint, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QMouseEvent, QPainter, QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from src.audio_capture import list_loopback_devices
from src.brain import Brain
from src.config import ROOT
from src.listener import Listener
from src.transcriber import Transcriber
from src.translator import Translator
from src.usage import UsageTracker

CONTEXT_FILE = ROOT / "context.txt"
DEVICE_FILE = ROOT / "audio_device.txt"
USAGE_FILE = ROOT / "usage.txt"
MODE_FILE = ROOT / "listen_mode.txt"  # "auto" or "controlled", remembered per user
GEOMETRY_FILE = ROOT / "window_geometry.txt"  # remembered window size + position
FONT_SIZE_FILE = ROOT / "font_size.txt"  # remembered answer/translation font size
LENGTH_FILE = ROOT / "answer_length.txt"  # remembered answer-length preference

# Answer/translation text zoom bounds (point size).
FONT_PT_DEFAULT = 11
FONT_PT_MIN = 8
FONT_PT_MAX = 28

# Official Google AI Studio page where the real quota/usage can be checked.
AI_STUDIO_USAGE_URL = "https://aistudio.google.com/"


class CopilotWorker(QThread):
    """Runs the listen -> transcribe -> answer -> translate loop off the UI thread."""

    question_detected = Signal(str)
    partial_text = Signal(str)  # live transcription of what's being heard right now
    answer_chunk = Signal(str)
    answer_done = Signal()
    translation_ready = Signal(str)
    exchange_recorded = Signal(str, str, str)  # question, answer, translation
    hearing = Signal(str)  # live capture state: idle / speech / transcribing
    usage_updated = Signal(int)  # new estimated request count for today
    status = Signal(str)

    def __init__(
        self,
        listener: Listener,
        brain: Brain,
        translator: Translator,
        usage: UsageTracker,
        context: str = "",
        mode: str = "auto",  # "auto" = VAD per-utterance, "controlled" = stop-to-send
    ) -> None:
        super().__init__()
        self.listener = listener
        self.brain = brain
        self.translator = translator
        self.usage = usage
        self.context = context
        self.mode = mode

    def run(self) -> None:
        self.status.emit("Escuchando...")
        # Update the briefing but KEEP memory, so pause/resume doesn't lose context.
        # Memory is only cleared via the "Nueva conversación" button.
        self.brain.set_context(self.context)
        # Controlled mode records everything until the user stops, then sends one
        # prompt; auto mode fires on every VAD-detected pause.
        if self.mode == "controlled":
            source = self.listener.capture_until_stop(
                on_state=self.hearing.emit, on_partial=self.partial_text.emit
            )
        else:
            source = self.listener.listen(
                on_state=self.hearing.emit, on_partial=self.partial_text.emit
            )
        try:
            for question in source:
                self.question_detected.emit(question)
                self.status.emit("Pensando...")
                # One answer = one real Gemini request. Count it (estimate).
                self.usage_updated.emit(self.usage.record())
                pieces: list[str] = []
                try:
                    for piece in self.brain.answer_stream(question):
                        pieces.append(piece)
                        self.answer_chunk.emit(piece)
                except Exception as exc:  # noqa: BLE001
                    detail = str(exc)
                    if "RESOURCE_EXHAUSTED" in detail or "429" in detail:
                        self.status.emit("⚠️ Límite de Gemini alcanzado")
                        self.answer_chunk.emit(
                            "\n⚠️ Alcanzaste el límite de pedidos de Gemini. "
                            "Espera un minuto (límite por minuto) o prueba mañana "
                            "(límite diario)."
                        )
                    else:
                        self.answer_chunk.emit(f"\n[error al consultar la IA: {exc}]")
                self.answer_done.emit()

                # Translate the finished answer offline (no tokens). Whole-text
                # translation only: partial sentences translate poorly.
                answer = "".join(pieces).strip()
                if answer:
                    self.status.emit("Traduciendo...")
                    translation = self.translator.translate(answer)
                    self.translation_ready.emit(translation)
                    self.exchange_recorded.emit(question, answer, translation)
                self.status.emit("Escuchando...")
        except Exception as exc:  # noqa: BLE001
            self.status.emit(f"Error: {exc}")

    def stop(self) -> None:
        self.listener.stop()


class ToggleSwitch(QCheckBox):
    """A sliding ON/OFF switch. Behaves like a checkbox but looks like a toggle."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(48, 26)

    def hitButton(self, pos) -> bool:
        # A plain QCheckBox only reacts on its tiny left indicator; make the WHOLE
        # painted switch clickable so tapping the right side toggles too.
        return self.rect().contains(pos)

    def paintEvent(self, event) -> None:  # noqa: ARG002 - Qt signature
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)

        radius = self.height() / 2
        track = QColor("#1a73e8") if self.isChecked() else QColor("#5f6368")
        painter.setBrush(track)
        painter.drawRoundedRect(0, 0, self.width(), self.height(), radius, radius)

        # White knob slides to the right when ON, left when OFF.
        diameter = self.height() - 6
        x = self.width() - diameter - 3 if self.isChecked() else 3
        painter.setBrush(QColor("#ffffff"))
        painter.drawEllipse(int(x), 3, diameter, diameter)


class Overlay(QWidget):
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
        self._drag_pos = None
        # Answer/translation text zoom, remembered across sessions.
        self._font_pt = self._load_font_size()
        # Answer-length preference, remembered across sessions.
        self._answer_length = self._load_length()
        self.brain.set_length(self._answer_length)
        # Whether the ES translation column is hidden (session-only view toggle).
        self._answer_only = False
        # Full Q&A log for the History window (not trimmed like the AI's memory).
        self.history_log: list[dict] = []
        self._history_dialog: QDialog | None = None
        self._history_view: QTextEdit | None = None
        self._history_search: QLineEdit | None = None
        self._history_filter = ""
        # Ticks a seconds counter while the answer is being prepared. A long
        # transcription is normal; a button that never changes reads as frozen.
        self._processing_secs = 0
        self._processing_timer = QTimer(self)
        self._processing_timer.timeout.connect(self._tick_processing)
        self._build_ui()

    def _build_ui(self) -> None:
        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.resize(820, 560)  # wide layout: answer (EN) + translation (ES) side by side
        self.setMinimumSize(460, 380)  # keep it usable when shrunk

        root = QWidget(self)
        root.setObjectName("root")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)  # card fills the window
        outer.addWidget(root)

        layout = QVBoxLayout(root)
        layout.setContentsMargins(16, 12, 16, 16)
        layout.setSpacing(10)

        # --- Title bar (draggable) ---
        bar = QHBoxLayout()
        title = QLabel("🎧 Copiloto")
        title.setFont(QFont("Segoe UI", 11, QFont.Bold))
        self.status_label = QLabel("Listo")
        self.status_label.setObjectName("status")

        # Estimated daily request count + a link to Google's official usage page.
        self.usage_label = QLabel("")
        self.usage_label.setObjectName("usage")
        self.usage_label.setToolTip(
            "Estimado de pedidos REALES de hoy (no es el número oficial de Google). "
            "Se reinicia a medianoche."
        )
        dash_btn = QPushButton("📊")
        dash_btn.setObjectName("dash")
        dash_btn.setFixedSize(26, 26)
        dash_btn.setToolTip("Ver el uso OFICIAL en Google AI Studio")
        dash_btn.clicked.connect(self._open_usage_dashboard)

        # Text zoom controls for the answer + translation panels.
        zoom_out_btn = QPushButton("A−")
        zoom_out_btn.setObjectName("zoom")
        zoom_out_btn.setFixedSize(26, 26)
        zoom_out_btn.setToolTip("Achicar el texto de la respuesta")
        zoom_out_btn.clicked.connect(lambda: self._zoom(-1))

        zoom_in_btn = QPushButton("A+")
        zoom_in_btn.setObjectName("zoom")
        zoom_in_btn.setFixedSize(26, 26)
        zoom_in_btn.setToolTip("Agrandar el texto de la respuesta")
        zoom_in_btn.clicked.connect(lambda: self._zoom(+1))

        close_btn = QPushButton("✕")
        close_btn.setObjectName("close")
        close_btn.setFixedSize(26, 26)
        close_btn.clicked.connect(self.close)
        bar.addWidget(title)
        bar.addStretch()
        bar.addWidget(zoom_out_btn)
        bar.addWidget(zoom_in_btn)
        bar.addWidget(self.usage_label)
        bar.addWidget(dash_btn)
        bar.addWidget(self.status_label)
        bar.addWidget(close_btn)
        layout.addLayout(bar)
        self._update_usage_label(self.usage.today_count())

        # --- Meeting context (briefing) ---
        ctx_label = QLabel("Contexto de la reunión (de qué se habla y cómo responder):")
        ctx_label.setObjectName("ctxlabel")
        ctx_label.setWordWrap(True)
        layout.addWidget(ctx_label)

        self.context_box = QTextEdit()
        self.context_box.setObjectName("context")
        self.context_box.setPlaceholderText(
            "Ej: Job interview for a virtual assistant role. I have 2 years of "
            "experience. Answer confidently, concise and professional."
        )
        self.context_box.setFont(QFont("Segoe UI", 10))
        self.context_box.setMaximumHeight(80)
        self.context_box.setPlainText(self._load_context())
        layout.addWidget(self.context_box)

        # --- Audio source selector ---
        dev_label = QLabel("Fuente de audio (qué salida escuchar):")
        dev_label.setObjectName("ctxlabel")
        layout.addWidget(dev_label)

        dev_row = QHBoxLayout()
        self.device_combo = QComboBox()
        self.device_combo.setObjectName("device")
        refresh_btn = QPushButton("⟳")
        refresh_btn.setObjectName("refresh")
        refresh_btn.setFixedSize(30, 28)
        refresh_btn.setToolTip("Volver a detectar dispositivos")
        refresh_btn.clicked.connect(self._populate_devices)
        dev_row.addWidget(self.device_combo, stretch=1)
        dev_row.addWidget(refresh_btn)
        layout.addLayout(dev_row)
        self._populate_devices()

        # --- Listening mode selector ---
        mode_label = QLabel("Modo de escucha:")
        mode_label.setObjectName("ctxlabel")
        layout.addWidget(mode_label)

        self.mode_combo = QComboBox()
        self.mode_combo.setObjectName("device")
        # userData is the value passed to the worker; label is what the user sees.
        self.mode_combo.addItem("🤖 Automático — responde en cada pausa", "auto")
        self.mode_combo.addItem(
            "✋ Controlado — junta todo y responde al Detener", "controlled"
        )
        self.mode_combo.setToolTip(
            "Automático: la IA responde cuando detecta una pausa (puede cortar si "
            "la persona duda).\nControlado: escucha todo hasta que pulsas Detener y "
            "recién ahí envía la pregunta completa a la IA."
        )
        self._select_mode(self._load_mode())
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        layout.addWidget(self.mode_combo)

        # --- Answer-length toggle (ON/OFF switch: OFF = short, ON = detailed) ---
        length_row = QHBoxLayout()
        length_row.setSpacing(8)
        length_title = QLabel("Longitud de la respuesta:")
        length_title.setObjectName("ctxlabel")
        self.length_off_label = QLabel("✂️ Cortas")
        self.length_off_label.setObjectName("switchlbl")
        self.length_switch = ToggleSwitch()
        self.length_switch.setChecked(self._answer_length == "detailed")
        self.length_switch.setToolTip(
            "Cortas: 1-2 frases, para decir rápido en vivo.\n"
            "Detalladas: respuesta completa y desarrollada."
        )
        self.length_switch.toggled.connect(self._on_length_toggled)
        self.length_on_label = QLabel("📖 Detalladas")
        self.length_on_label.setObjectName("switchlbl")
        length_row.addWidget(length_title)
        length_row.addStretch()
        length_row.addWidget(self.length_off_label)
        length_row.addWidget(self.length_switch)
        length_row.addWidget(self.length_on_label)
        layout.addLayout(length_row)
        self._update_length_labels()

        # --- Toggle + new-conversation buttons ---
        btn_row = QHBoxLayout()
        self.toggle_btn = QPushButton("● Escuchar")
        self.toggle_btn.setObjectName("toggle")
        self.toggle_btn.setFixedHeight(40)
        self.toggle_btn.clicked.connect(self._toggle)

        self.history_btn = QPushButton("📜 Historial")
        self.history_btn.setObjectName("history")
        self.history_btn.setFixedHeight(40)
        self.history_btn.setToolTip("Ver todas las preguntas y respuestas de esta conversación")
        self.history_btn.clicked.connect(self._open_history)

        self.new_conv_btn = QPushButton("🗑️ Nueva conversación")
        self.new_conv_btn.setObjectName("newconv")
        self.new_conv_btn.setFixedHeight(40)
        self.new_conv_btn.setToolTip("Borra la memoria y empieza una conversación de cero")
        self.new_conv_btn.clicked.connect(self._new_conversation)

        btn_row.addWidget(self.toggle_btn, stretch=1)
        btn_row.addWidget(self.history_btn)
        btn_row.addWidget(self.new_conv_btn)
        layout.addLayout(btn_row)

        # --- Detected question ---
        self.question_label = QLabel("La pregunta detectada aparecerá acá.")
        self.question_label.setObjectName("question")
        self.question_label.setWordWrap(True)
        layout.addWidget(self.question_label)

        # --- Answer (EN) and translation (ES), side by side ---
        answers_row = QHBoxLayout()
        answers_row.setSpacing(10)

        # English column, wrapped in a container so the layout can hide/show it.
        en_container = QWidget()
        en_col = QVBoxLayout(en_container)
        en_col.setContentsMargins(0, 0, 0, 0)
        en_col.setSpacing(4)
        en_header_row = QHBoxLayout()
        en_header = QLabel("🗣️ Respuesta (EN) — lo que dices")
        en_header.setObjectName("colheader")
        # Toggle to hide the ES column and read the answer full width. Lives in the
        # EN header so it stays reachable even when the ES column is hidden.
        self.answer_only_btn = QPushButton("👁️ ES")
        self.answer_only_btn.setObjectName("zoom")
        self.answer_only_btn.setFixedHeight(22)
        self.answer_only_btn.setToolTip("Mostrar/ocultar la columna de traducción (ES)")
        self.answer_only_btn.clicked.connect(self._toggle_answer_only)
        en_header_row.addWidget(en_header)
        en_header_row.addStretch()
        en_header_row.addWidget(self.answer_only_btn)
        self.answer_box = QTextEdit()
        self.answer_box.setReadOnly(True)
        self.answer_box.setFont(QFont("Segoe UI", self._font_pt))
        en_col.addLayout(en_header_row)
        en_col.addWidget(self.answer_box, stretch=1)

        # Spanish column, also wrapped so it can be toggled off entirely.
        self.es_container = QWidget()
        es_col = QVBoxLayout(self.es_container)
        es_col.setContentsMargins(0, 0, 0, 0)
        es_col.setSpacing(4)
        es_header = QLabel("👁️ Traducción (ES) — para entender")
        es_header.setObjectName("colheader")
        self.translation_box = QTextEdit()
        self.translation_box.setReadOnly(True)
        self.translation_box.setFont(QFont("Segoe UI", self._font_pt))
        es_col.addWidget(es_header)
        es_col.addWidget(self.translation_box, stretch=1)

        # Spanish on the left (support); English on the right as the wider, tall
        # primary column — it's the answer the user actually says out loud.
        answers_row.addWidget(self.es_container, stretch=1)
        answers_row.addWidget(en_container, stretch=2)
        layout.addLayout(answers_row, stretch=1)

        # --- Live audio/understanding status (bottom) ---
        self.heard_label = QLabel("🎧 Sin escuchar todavía.")
        self.heard_label.setObjectName("heard")
        self.heard_label.setWordWrap(True)
        layout.addWidget(self.heard_label)

        self.setStyleSheet(
            """
            #root {
                background-color: rgba(20, 22, 28, 235);
                border: 1px solid rgba(120, 130, 150, 90);
                border-radius: 14px;
            }
            QLabel { color: #e8eaed; }
            #status { color: #9aa0a6; font-size: 11px; }
            #usage { color: #81c995; font-size: 11px; }
            QPushButton#dash {
                background-color: transparent; color: #9aa0a6;
                border: none; font-size: 13px;
            }
            QPushButton#dash:hover { color: #8ab4f8; }
            QPushButton#zoom {
                background-color: rgba(255,255,255,18); color: #e8eaed;
                border: 1px solid rgba(120,130,150,70); border-radius: 6px;
                font-size: 11px; font-weight: bold;
            }
            QPushButton#zoom:hover { background-color: rgba(255,255,255,32); }
            #question {
                color: #8ab4f8; font-size: 12px; font-style: italic;
                padding: 6px 0;
            }
            #ctxlabel { color: #9aa0a6; font-size: 11px; }
            #colheader { color: #9aa0a6; font-size: 11px; font-weight: bold; }
            #heard {
                color: #fdd663; font-size: 11px; font-style: italic;
                padding: 4px 0;
            }
            QTextEdit#context {
                background-color: rgba(255,255,255,18); color: #e8eaed;
                border: 1px solid rgba(120,130,150,70); border-radius: 8px;
                padding: 6px;
            }
            QPushButton#toggle {
                background-color: #1a73e8; color: white; border: none;
                border-radius: 8px; font-size: 13px; font-weight: bold;
            }
            QPushButton#toggle:hover { background-color: #1b66c9; }
            QPushButton#newconv {
                background-color: rgba(255,255,255,18); color: #e8eaed;
                border: 1px solid rgba(120,130,150,70); border-radius: 8px;
                font-size: 12px; padding: 0 12px;
            }
            QPushButton#newconv:hover { background-color: rgba(242,139,130,40); }
            QPushButton#history {
                background-color: rgba(255,255,255,18); color: #e8eaed;
                border: 1px solid rgba(120,130,150,70); border-radius: 8px;
                font-size: 12px; padding: 0 12px;
            }
            QPushButton#history:hover { background-color: rgba(138,180,248,40); }
            QPushButton#close {
                background-color: transparent; color: #9aa0a6;
                border: none; font-size: 14px;
            }
            QPushButton#close:hover { color: #f28b82; }
            QComboBox#device {
                background-color: rgba(255,255,255,18); color: #e8eaed;
                border: 1px solid rgba(120,130,150,70); border-radius: 8px;
                padding: 5px 8px; font-size: 11px;
            }
            QComboBox#device QAbstractItemView {
                background-color: #1a1c22; color: #e8eaed;
                selection-background-color: #1a73e8;
            }
            QPushButton#refresh {
                background-color: rgba(255,255,255,18); color: #e8eaed;
                border: 1px solid rgba(120,130,150,70); border-radius: 8px;
                font-size: 14px;
            }
            QPushButton#refresh:hover { background-color: rgba(255,255,255,32); }
            QTextEdit {
                background-color: rgba(255,255,255,12); color: #e8eaed;
                border: none; border-radius: 8px; padding: 8px;
            }
            """
        )

        # Restore the last window size + position, if any.
        self._load_geometry()

    # --- Text zoom (answer + translation panels) ---
    def _zoom(self, delta: int) -> None:
        self._font_pt = max(FONT_PT_MIN, min(FONT_PT_MAX, self._font_pt + delta))
        font = QFont("Segoe UI", self._font_pt)
        self.answer_box.setFont(font)
        self.translation_box.setFont(font)
        self._save_font_size(self._font_pt)

    def _load_font_size(self) -> int:
        try:
            pt = int(FONT_SIZE_FILE.read_text(encoding="utf-8").strip())
            return max(FONT_PT_MIN, min(FONT_PT_MAX, pt))
        except (OSError, ValueError):
            return FONT_PT_DEFAULT

    def _save_font_size(self, pt: int) -> None:
        try:
            FONT_SIZE_FILE.write_text(str(pt), encoding="utf-8")
        except OSError:
            pass

    # --- Answer length (short vs detailed) ---
    def _on_length_toggled(self, detailed: bool) -> None:
        self._answer_length = "detailed" if detailed else "short"
        self.brain.set_length(self._answer_length)
        self._save_length(self._answer_length)
        self._update_length_labels()

    def _update_length_labels(self) -> None:
        """Highlight the active side so the switch reads clearly."""
        detailed = self._answer_length == "detailed"
        active = "color:#e8eaed; font-weight:bold;"
        dim = "color:#7a7f8a;"
        self.length_off_label.setStyleSheet(dim if detailed else active)
        self.length_on_label.setStyleSheet(active if detailed else dim)

    def _load_length(self) -> str:
        try:
            value = LENGTH_FILE.read_text(encoding="utf-8").strip()
            return value if value in ("short", "detailed") else "short"
        except OSError:
            return "short"

    def _save_length(self, length: str) -> None:
        try:
            LENGTH_FILE.write_text(length, encoding="utf-8")
        except OSError:
            pass

    # --- Answer-only view (hide the ES translation column) ---
    def _toggle_answer_only(self) -> None:
        self._answer_only = not self._answer_only
        self.es_container.setVisible(not self._answer_only)
        self.answer_only_btn.setText("👁️‍🗨️ ES" if self._answer_only else "👁️ ES")

    # --- Window geometry persistence ---
    def _load_geometry(self) -> None:
        try:
            raw = GEOMETRY_FILE.read_text(encoding="utf-8").strip()
            x, y, w, h = (int(v) for v in raw.split(","))
        except (OSError, ValueError):
            return
        w = max(self.minimumWidth(), w)
        h = max(self.minimumHeight(), h)
        self.setGeometry(x, y, w, h)

    def _save_geometry(self) -> None:
        geo = self.geometry()
        try:
            GEOMETRY_FILE.write_text(
                f"{geo.x()},{geo.y()},{geo.width()},{geo.height()}", encoding="utf-8"
            )
        except OSError:
            pass

    # --- Listening control ---
    def _toggle(self) -> None:
        if self.worker and self.worker.isRunning():
            if self._selected_mode() == "controlled":
                self._finish_controlled()
            else:
                self._stop_listening()
        else:
            self._start_listening()

    def _new_conversation(self) -> None:
        """Wipe the AI's memory and start fresh (keeps listening if active)."""
        context = self.context_box.toPlainText().strip()
        self.brain.reset(context)
        self.history_log = []  # the history window is tied to the conversation
        self._refresh_history_view()
        self.answer_box.clear()
        self.translation_box.clear()
        self.question_label.setText("Conversación reiniciada. Esperando preguntas...")
        self.heard_label.setText("🗑️ Memoria e historial borrados. Empezamos de cero.")

    def _load_context(self) -> str:
        try:
            return CONTEXT_FILE.read_text(encoding="utf-8")
        except OSError:
            return ""

    def _save_context(self, text: str) -> None:
        try:
            CONTEXT_FILE.write_text(text, encoding="utf-8")
        except OSError:
            pass

    # --- Audio device selection ---
    def _populate_devices(self) -> None:
        """Fill the dropdown with available loopback devices.

        Preselects the device saved from last time, or the system default.
        Each item stores its device dict (index + name) as item data.
        """
        self.device_combo.clear()
        try:
            devices = list_loopback_devices()
        except Exception as exc:  # noqa: BLE001
            self.device_combo.addItem(f"(sin dispositivos: {exc})", None)
            return

        if not devices:
            self.device_combo.addItem("(no se detectaron dispositivos)", None)
            return

        saved = self._load_device_name()
        selected = 0
        for i, dev in enumerate(devices):
            label = dev["name"] + (" — predeterminado" if dev["is_default"] else "")
            self.device_combo.addItem(label, dev)
            if saved and dev["name"] == saved:
                selected = i
            elif not saved and dev["is_default"]:
                selected = i
        self.device_combo.setCurrentIndex(selected)

    def _selected_device(self) -> dict | None:
        return self.device_combo.currentData()

    def _load_device_name(self) -> str:
        try:
            return DEVICE_FILE.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def _save_device_name(self, name: str) -> None:
        try:
            DEVICE_FILE.write_text(name, encoding="utf-8")
        except OSError:
            pass

    # --- Listening mode selection ---
    def _selected_mode(self) -> str:
        return self.mode_combo.currentData() or "auto"

    def _select_mode(self, mode: str) -> None:
        index = self.mode_combo.findData(mode)
        self.mode_combo.setCurrentIndex(index if index >= 0 else 0)

    def _load_mode(self) -> str:
        try:
            return MODE_FILE.read_text(encoding="utf-8").strip() or "auto"
        except OSError:
            return "auto"

    def _save_mode(self, mode: str) -> None:
        try:
            MODE_FILE.write_text(mode, encoding="utf-8")
        except OSError:
            pass

    def _on_mode_changed(self) -> None:
        """Persist the choice and reflect it in the idle toggle-button label."""
        self._save_mode(self._selected_mode())
        if not (self.worker and self.worker.isRunning()):
            self.toggle_btn.setText(self._idle_toggle_label())

    def _idle_toggle_label(self) -> str:
        return "● Escuchar"

    def _start_listening(self) -> None:
        self.answer_box.clear()
        self.translation_box.clear()
        self.question_label.setText("Escuchando la llamada...")
        self.heard_label.setText("🎧 Escuchando la llamada...")
        context = self.context_box.toPlainText().strip()
        self._save_context(context)  # remember it for next time

        device = self._selected_device()
        device_index = device["index"] if device else None
        if device:
            self._save_device_name(device["name"])  # remember the choice

        mode = self._selected_mode()
        listener = Listener(self.transcriber, device_index=device_index)
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
        # Lock the mode while a capture is running so it can't change mid-flight.
        self.mode_combo.setEnabled(False)
        if mode == "controlled":
            self.toggle_btn.setText("■ Detener y responder")
        else:
            self.toggle_btn.setText("■ Detener")

    def _finish_controlled(self) -> None:
        """Controlled mode: stop capturing and let the worker transcribe the whole
        recording and answer. Non-blocking — the answer streams while we wait."""
        if self.worker:
            self.worker.stop()
        self._processing_secs = 0
        self.toggle_btn.setText("⏳ Procesando... 0s")
        self.toggle_btn.setEnabled(False)
        self.status_label.setText("Procesando lo escuchado...")
        self._processing_timer.start(1000)

    def _tick_processing(self) -> None:
        """Keep the disabled button visibly counting so a long wait still reads
        as work in progress rather than a hang."""
        self._processing_secs += 1
        self.toggle_btn.setText(f"⏳ Procesando... {self._processing_secs}s")

    def _stop_listening(self) -> None:
        if self.worker:
            self.worker.stop()
            self.worker.wait(3000)
        self.toggle_btn.setText(self._idle_toggle_label())
        self.status_label.setText("Listo")

    def _on_worker_finished(self) -> None:
        """Reset the UI once the worker thread ends (both modes)."""
        self._processing_timer.stop()
        self.mode_combo.setEnabled(True)
        self.toggle_btn.setEnabled(True)
        self.toggle_btn.setText(self._idle_toggle_label())
        self.status_label.setText("Listo")

    # --- Signal handlers (run on the UI thread) ---
    def _on_question(self, text: str) -> None:
        self.question_label.setText(f"❓ {text}")
        self.heard_label.setText(f"👂 Escuché: {text}")
        self.answer_box.clear()
        self.translation_box.clear()

    def _on_partial(self, text: str) -> None:
        """Live transcription of what's being heard, before the question is final."""
        self.question_label.setText(f"👂 {text}")

    def _update_usage_label(self, count: int) -> None:
        self.usage_label.setText(f"📨 ~{count} hoy (est.)")

    # --- Conversation history window ---
    def _on_exchange_recorded(self, question: str, answer: str, translation: str) -> None:
        self.history_log.append(
            {"question": question, "answer": answer, "translation": translation}
        )
        self._refresh_history_view()

    def _history_html(self) -> str:
        if not self.history_log:
            return (
                "<p style='color:#9aa0a6'>Todavía no hay preguntas en esta "
                "conversación.</p>"
            )
        needle = self._history_filter.strip().lower()
        blocks = []
        for i, entry in enumerate(self.history_log, 1):
            # Filter across question + answer + translation (case-insensitive).
            if needle and needle not in (
                f"{entry['question']} {entry['answer']} {entry['translation']}".lower()
            ):
                continue
            q = html.escape(entry["question"])
            a = html.escape(entry["answer"]).replace("\n", "<br>")
            t = html.escape(entry["translation"]).replace("\n", "<br>")
            blocks.append(
                f"<p style='color:#8ab4f8'><b>#{i} &#10067; {q}</b></p>"
                f"<p style='color:#e8eaed'>&#128483;&#65039; {a}</p>"
                f"<p style='color:#81c995'>&#128065;&#65039; {t}</p>"
                "<hr style='border:none;border-top:1px solid #3a3f4b'>"
            )
        if not blocks:
            return (
                "<p style='color:#9aa0a6'>Sin resultados para "
                f"«{html.escape(self._history_filter)}».</p>"
            )
        # Most recent first: newest exchange shows at the top, no scrolling needed.
        return "".join(reversed(blocks))

    def _refresh_history_view(self) -> None:
        if self._history_view is not None and self._history_dialog is not None:
            if self._history_dialog.isVisible():
                self._history_view.setHtml(self._history_html())

    def _open_history(self) -> None:
        if self._history_dialog is None:
            dlg = QDialog(self)
            dlg.setWindowTitle("Historial de la conversación")
            dlg.resize(560, 600)
            dlg.setStyleSheet(
                "QDialog { background-color: #14161c; }"
                "QTextEdit { background-color: #1a1c22; color: #e8eaed;"
                " border: none; padding: 8px; }"
            )
            lay = QVBoxLayout(dlg)
            lay.setContentsMargins(10, 10, 10, 10)
            search = QLineEdit()
            search.setPlaceholderText("🔎 Buscar en la conversación...")
            search.setClearButtonEnabled(True)
            search.setStyleSheet(
                "QLineEdit { background-color: #1a1c22; color: #e8eaed;"
                " border: 1px solid #3a3f4b; border-radius: 6px; padding: 6px; }"
            )
            search.setText(self._history_filter)
            search.textChanged.connect(self._on_history_search)
            lay.addWidget(search)
            view = QTextEdit()
            view.setReadOnly(True)
            view.setFont(QFont("Segoe UI", 10))
            lay.addWidget(view)
            self._history_dialog = dlg
            self._history_view = view
            self._history_search = search

        self._history_view.setHtml(self._history_html())
        self._history_dialog.show()
        self._history_dialog.raise_()
        self._history_dialog.activateWindow()

    def _on_history_search(self, text: str) -> None:
        self._history_filter = text
        self._refresh_history_view()

    def _open_usage_dashboard(self) -> None:
        webbrowser.open(AI_STUDIO_USAGE_URL)

    def _on_hearing(self, state: str) -> None:
        messages = {
            "idle": "🎧 Escuchando la llamada...",
            "speech": "🎤 La otra persona está hablando...",
            "capturing": "🔴 Grabando todo... pulsa «Detener» cuando termine la pregunta.",
            "transcribing": "🧠 Entendiendo lo que dijo...",
        }
        self.heard_label.setText(messages.get(state, "🎧 Escuchando la llamada..."))

    def _on_chunk(self, text: str) -> None:
        cursor = self.answer_box.textCursor()
        cursor.movePosition(QTextCursor.End)
        self.answer_box.setTextCursor(cursor)
        self.answer_box.insertPlainText(text)
        self.answer_box.ensureCursorVisible()

    def _on_translation(self, text: str) -> None:
        self.translation_box.setPlainText(text)

    # --- Frameless window: drag the body to move; edges resize via WM_NCHITTEST ---
    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._drag_pos and event.buttons() & Qt.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_pos)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if self._drag_pos is not None:
            self._save_geometry()  # remember the new position after a drag
        self._drag_pos = None

    def nativeEvent(self, eventType, message):
        """Let Windows resize this frameless window by claiming the outer edges as
        the resize border. Handling WM_NCHITTEST works below Qt's event dispatch,
        so child widgets can't swallow it and Windows supplies the resize cursors.
        """
        if eventType in (b"windows_generic_MSG", "windows_generic_MSG"):
            msg = wintypes.MSG.from_address(int(message))
            if msg.message == 0x0084:  # WM_NCHITTEST
                gx = ctypes.c_int16(msg.lParam & 0xFFFF).value  # signed for multi-monitor
                gy = ctypes.c_int16((msg.lParam >> 16) & 0xFFFF).value
                pos = self.mapFromGlobal(QPoint(gx, gy))
                w, h, m = self.width(), self.height(), RESIZE_MARGIN
                left, right = pos.x() < m, pos.x() >= w - m
                top, bottom = pos.y() < m, pos.y() >= h - m
                # HT* codes: TL=13 TR=14 BL=16 BR=17 L=10 R=11 T=12 B=15
                if top and left:
                    return True, 13
                if top and right:
                    return True, 14
                if bottom and left:
                    return True, 16
                if bottom and right:
                    return True, 17
                if left:
                    return True, 10
                if right:
                    return True, 11
                if top:
                    return True, 12
                if bottom:
                    return True, 15
        return super().nativeEvent(eventType, message)

    def showEvent(self, event) -> None:
        """When the window appears, exclude it from screen capture (Windows)."""
        super().showEvent(event)
        self._enable_stealth()

    def _enable_stealth(self) -> None:
        if not HIDE_FROM_SCREENSHARE:
            return
        if sys.platform != "win32":
            return
        try:
            hwnd = int(self.winId())
            ok = ctypes.windll.user32.SetWindowDisplayAffinity(
                hwnd, WDA_EXCLUDEFROMCAPTURE
            )
            if ok:
                self.status_label.setText("Oculto en captura")
                print("[OK] Modo invisible para screen-share activado.")
            else:
                print("[!] No se pudo activar el modo invisible (Windows viejo?).")
        except Exception as exc:  # noqa: BLE001
            print(f"[!] Stealth no disponible: {exc}")

    def closeEvent(self, event) -> None:
        self._save_geometry()  # persist size/position on exit
        self._stop_listening()
        event.accept()


def main() -> None:
    app = QApplication(sys.argv)

    print("Cargando modelo de transcripcion (puede tardar unos segundos)...")
    transcriber = Transcriber()
    print(f"[OK] Whisper en: {transcriber.device}")
    brain = Brain()
    print("[OK] Gemini listo.")
    print("Preparando traductor offline (descarga el modelo la primera vez)...")
    translator = Translator()
    print(f"[OK] Traductor offline listo: {translator.ready}. Abriendo ventana...")

    usage = UsageTracker(USAGE_FILE)
    window = Overlay(transcriber, brain, translator, usage)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
