# Copiloto de Llamada

Real-time call copilot for Windows. It listens to your **system audio** (what plays
through your speakers), transcribes speech locally on the GPU, detects when the other
person finishes a question, and streams an AI answer into a floating overlay window.

Built for live video calls (Meet, Zoom, etc.): the other person asks a question, and
you get a ready-to-say answer in seconds.

## How it works

1. **Capture** — system audio via WASAPI loopback (`pyaudiowpatch`)
2. **Transcribe** — `faster-whisper` on the GPU (CUDA), falls back to CPU
3. **Endpointing** — detects the end of a question by silence (`webrtcvad`)
4. **Answer** — Google Gemini streams the answer (`google-genai`)
5. **UI** — always-on-top overlay (`PySide6`)

## Requirements

- Windows 10/11
- Python 3.12
- An NVIDIA GPU with CUDA is recommended (works on CPU too, slower)
- A free Gemini API key from [Google AI Studio](https://aistudio.google.com)

## Setup

```bash
# 1. Create and activate a virtual environment
py -3.12 -m venv .venv
.venv\Scripts\activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. (GPU only) install the CUDA runtime libraries
pip install nvidia-cublas-cu12 nvidia-cudnn-cu12
```

Create a `.env` file in the project root with your API key:

```
GEMINI_API_KEY=your_key_here
```

## Run

```bash
python -m src.main
```

Or double-click `run.bat`.

Type the meeting context (topic + how you want to answer) in the box, click
**Escuchar**, and the copilot starts listening.

## Configuration

All settings live in `src/config.py`: model, language, Whisper size, silence
threshold. The screen-share invisibility switch (`HIDE_FROM_SCREENSHARE`) is in
`src/main.py`.
