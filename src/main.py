"""Floating overlay copilot.

Click "Escuchar" to start listening to system audio. When the other person on the
call finishes a question (detected by a pause), it is transcribed and Gemini's
answer streams into the window in real time.
"""
from __future__ import annotations

import ctypes
import html
import sys
import webbrowser

# Windows: hide the window from screen capture / screen share while keeping it
# visible to the local user. Requires Windows 10 2004+.
WDA_EXCLUDEFROMCAPTURE = 0x00000011

# Switch: True = invisible in screen-share/screenshots. False = normal window.
HIDE_FROM_SCREENSHARE = False

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QFont, QMouseEvent, QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
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

# Official Google AI Studio page where the real quota/usage can be checked.
AI_STUDIO_USAGE_URL = "https://aistudio.google.com/"


class CopilotWorker(QThread):
    """Runs the listen -> transcribe -> answer -> translate loop off the UI thread."""

    question_detected = Signal(str)
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
    ) -> None:
        super().__init__()
        self.listener = listener
        self.brain = brain
        self.translator = translator
        self.usage = usage
        self.context = context

    def run(self) -> None:
        self.status.emit("Escuchando...")
        # Update the briefing but KEEP memory, so pause/resume doesn't lose context.
        # Memory is only cleared via the "Nueva conversación" button.
        self.brain.set_context(self.context)
        try:
            for question in self.listener.listen(on_state=self.hearing.emit):
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
        # Full Q&A log for the History window (not trimmed like the AI's memory).
        self.history_log: list[dict] = []
        self._history_dialog: QDialog | None = None
        self._history_view: QTextEdit | None = None
        self._build_ui()

    def _build_ui(self) -> None:
        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.resize(820, 560)  # wide layout: answer (EN) + translation (ES) side by side

        root = QWidget(self)
        root.setObjectName("root")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
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

        close_btn = QPushButton("✕")
        close_btn.setObjectName("close")
        close_btn.setFixedSize(26, 26)
        close_btn.clicked.connect(self.close)
        bar.addWidget(title)
        bar.addStretch()
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

        en_col = QVBoxLayout()
        en_col.setSpacing(4)
        en_header = QLabel("🗣️ Respuesta (EN) — lo que dices")
        en_header.setObjectName("colheader")
        self.answer_box = QTextEdit()
        self.answer_box.setReadOnly(True)
        self.answer_box.setFont(QFont("Segoe UI", 11))
        en_col.addWidget(en_header)
        en_col.addWidget(self.answer_box, stretch=1)

        es_col = QVBoxLayout()
        es_col.setSpacing(4)
        es_header = QLabel("👁️ Traducción (ES) — para entender")
        es_header.setObjectName("colheader")
        self.translation_box = QTextEdit()
        self.translation_box.setReadOnly(True)
        self.translation_box.setFont(QFont("Segoe UI", 11))
        es_col.addWidget(es_header)
        es_col.addWidget(self.translation_box, stretch=1)

        # Spanish on the left (support); English on the right as the wider, tall
        # primary column — it's the answer the user actually says out loud.
        answers_row.addLayout(es_col, stretch=1)
        answers_row.addLayout(en_col, stretch=2)
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

    # --- Listening control ---
    def _toggle(self) -> None:
        if self.worker and self.worker.isRunning():
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

        listener = Listener(self.transcriber, device_index=device_index)
        self.worker = CopilotWorker(
            listener, self.brain, self.translator, self.usage, context
        )
        self.worker.question_detected.connect(self._on_question)
        self.worker.answer_chunk.connect(self._on_chunk)
        self.worker.translation_ready.connect(self._on_translation)
        self.worker.exchange_recorded.connect(self._on_exchange_recorded)
        self.worker.hearing.connect(self._on_hearing)
        self.worker.usage_updated.connect(self._update_usage_label)
        self.worker.status.connect(self.status_label.setText)
        self.worker.start()
        self.toggle_btn.setText("■ Detener")

    def _stop_listening(self) -> None:
        if self.worker:
            self.worker.stop()
            self.worker.wait(3000)
        self.toggle_btn.setText("● Escuchar")
        self.status_label.setText("Listo")

    # --- Signal handlers (run on the UI thread) ---
    def _on_question(self, text: str) -> None:
        self.question_label.setText(f"❓ {text}")
        self.heard_label.setText(f"👂 Escuché: {text}")
        self.answer_box.clear()
        self.translation_box.clear()

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
        blocks = []
        for i, entry in enumerate(self.history_log, 1):
            q = html.escape(entry["question"])
            a = html.escape(entry["answer"]).replace("\n", "<br>")
            t = html.escape(entry["translation"]).replace("\n", "<br>")
            blocks.append(
                f"<p style='color:#8ab4f8'><b>#{i} &#10067; {q}</b></p>"
                f"<p style='color:#e8eaed'>&#128483;&#65039; {a}</p>"
                f"<p style='color:#81c995'>&#128065;&#65039; {t}</p>"
                "<hr style='border:none;border-top:1px solid #3a3f4b'>"
            )
        return "".join(blocks)

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
            view = QTextEdit()
            view.setReadOnly(True)
            view.setFont(QFont("Segoe UI", 10))
            lay.addWidget(view)
            self._history_dialog = dlg
            self._history_view = view

        self._history_view.setHtml(self._history_html())
        self._history_dialog.show()
        self._history_dialog.raise_()
        self._history_dialog.activateWindow()

    def _open_usage_dashboard(self) -> None:
        webbrowser.open(AI_STUDIO_USAGE_URL)

    def _on_hearing(self, state: str) -> None:
        messages = {
            "idle": "🎧 Escuchando la llamada...",
            "speech": "🎤 La otra persona está hablando...",
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

    # --- Make the frameless window draggable ---
    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._drag_pos and event.buttons() & Qt.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_pos)

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
