"""Floating overlay copilot.

Click "Escuchar" to start listening to system audio. When the other person on the
call finishes a question (detected by a pause), it is transcribed and Gemini's
answer streams into the window in real time.
"""
from __future__ import annotations

import ctypes
import sys

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

CONTEXT_FILE = ROOT / "context.txt"
DEVICE_FILE = ROOT / "audio_device.txt"


class CopilotWorker(QThread):
    """Runs the listen -> transcribe -> answer -> translate loop off the UI thread."""

    question_detected = Signal(str)
    answer_chunk = Signal(str)
    answer_done = Signal()
    translation_ready = Signal(str)
    status = Signal(str)

    def __init__(
        self,
        listener: Listener,
        brain: Brain,
        translator: Translator,
        context: str = "",
    ) -> None:
        super().__init__()
        self.listener = listener
        self.brain = brain
        self.translator = translator
        self.context = context

    def run(self) -> None:
        self.status.emit("Escuchando...")
        try:
            for question in self.listener.listen():
                self.question_detected.emit(question)
                self.status.emit("Pensando...")
                pieces: list[str] = []
                try:
                    for piece in self.brain.answer_stream(question, self.context):
                        pieces.append(piece)
                        self.answer_chunk.emit(piece)
                except Exception as exc:  # noqa: BLE001
                    self.answer_chunk.emit(f"\n[error al consultar la IA: {exc}]")
                self.answer_done.emit()

                # Translate the finished answer offline (no tokens). Whole-text
                # translation only: partial sentences translate poorly.
                answer = "".join(pieces).strip()
                if answer:
                    self.status.emit("Traduciendo...")
                    self.translation_ready.emit(self.translator.translate(answer))
                self.status.emit("Escuchando...")
        except Exception as exc:  # noqa: BLE001
            self.status.emit(f"Error: {exc}")

    def stop(self) -> None:
        self.listener.stop()


class Overlay(QWidget):
    def __init__(
        self, transcriber: Transcriber, brain: Brain, translator: Translator
    ) -> None:
        super().__init__()
        self.transcriber = transcriber
        self.brain = brain
        self.translator = translator
        self.worker: CopilotWorker | None = None
        self._drag_pos = None
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
        close_btn = QPushButton("✕")
        close_btn.setObjectName("close")
        close_btn.setFixedSize(26, 26)
        close_btn.clicked.connect(self.close)
        bar.addWidget(title)
        bar.addStretch()
        bar.addWidget(self.status_label)
        bar.addWidget(close_btn)
        layout.addLayout(bar)

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

        # --- Toggle button ---
        self.toggle_btn = QPushButton("● Escuchar")
        self.toggle_btn.setObjectName("toggle")
        self.toggle_btn.setFixedHeight(40)
        self.toggle_btn.clicked.connect(self._toggle)
        layout.addWidget(self.toggle_btn)

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

        self.setStyleSheet(
            """
            #root {
                background-color: rgba(20, 22, 28, 235);
                border: 1px solid rgba(120, 130, 150, 90);
                border-radius: 14px;
            }
            QLabel { color: #e8eaed; }
            #status { color: #9aa0a6; font-size: 11px; }
            #question {
                color: #8ab4f8; font-size: 12px; font-style: italic;
                padding: 6px 0;
            }
            #ctxlabel { color: #9aa0a6; font-size: 11px; }
            #colheader { color: #9aa0a6; font-size: 11px; font-weight: bold; }
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
        context = self.context_box.toPlainText().strip()
        self._save_context(context)  # remember it for next time

        device = self._selected_device()
        device_index = device["index"] if device else None
        if device:
            self._save_device_name(device["name"])  # remember the choice

        listener = Listener(self.transcriber, device_index=device_index)
        self.worker = CopilotWorker(listener, self.brain, self.translator, context)
        self.worker.question_detected.connect(self._on_question)
        self.worker.answer_chunk.connect(self._on_chunk)
        self.worker.translation_ready.connect(self._on_translation)
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
        self.answer_box.clear()
        self.translation_box.clear()

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

    window = Overlay(transcriber, brain, translator)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
