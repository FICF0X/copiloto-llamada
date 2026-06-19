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
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from src.brain import Brain
from src.config import ROOT
from src.listener import Listener
from src.transcriber import Transcriber

CONTEXT_FILE = ROOT / "context.txt"


class CopilotWorker(QThread):
    """Runs the listen -> transcribe -> answer loop off the UI thread."""

    question_detected = Signal(str)
    answer_chunk = Signal(str)
    answer_done = Signal()
    status = Signal(str)

    def __init__(self, listener: Listener, brain: Brain, context: str = "") -> None:
        super().__init__()
        self.listener = listener
        self.brain = brain
        self.context = context

    def run(self) -> None:
        self.status.emit("Escuchando...")
        try:
            for question in self.listener.listen():
                self.question_detected.emit(question)
                self.status.emit("Pensando...")
                try:
                    for piece in self.brain.answer_stream(question, self.context):
                        self.answer_chunk.emit(piece)
                except Exception as exc:  # noqa: BLE001
                    self.answer_chunk.emit(f"\n[error al consultar la IA: {exc}]")
                self.answer_done.emit()
                self.status.emit("Escuchando...")
        except Exception as exc:  # noqa: BLE001
            self.status.emit(f"Error: {exc}")

    def stop(self) -> None:
        self.listener.stop()


class Overlay(QWidget):
    def __init__(self, transcriber: Transcriber, brain: Brain) -> None:
        super().__init__()
        self.transcriber = transcriber
        self.brain = brain
        self.worker: CopilotWorker | None = None
        self._drag_pos = None
        self._build_ui()

    def _build_ui(self) -> None:
        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.resize(460, 540)

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

        # --- Streaming answer ---
        self.answer_box = QTextEdit()
        self.answer_box.setReadOnly(True)
        self.answer_box.setFont(QFont("Segoe UI", 11))
        layout.addWidget(self.answer_box, stretch=1)

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

    def _start_listening(self) -> None:
        self.answer_box.clear()
        self.question_label.setText("Escuchando la llamada...")
        context = self.context_box.toPlainText().strip()
        self._save_context(context)  # remember it for next time
        listener = Listener(self.transcriber)
        self.worker = CopilotWorker(listener, self.brain, context)
        self.worker.question_detected.connect(self._on_question)
        self.worker.answer_chunk.connect(self._on_chunk)
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

    def _on_chunk(self, text: str) -> None:
        cursor = self.answer_box.textCursor()
        cursor.movePosition(QTextCursor.End)
        self.answer_box.setTextCursor(cursor)
        self.answer_box.insertPlainText(text)
        self.answer_box.ensureCursorVisible()

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
    print("[OK] Gemini listo. Abriendo ventana...")

    window = Overlay(transcriber, brain)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
