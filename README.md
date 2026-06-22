# 🎧 Copiloto de Llamada

> Real-time AI copilot that listens to your call, understands the questions, and streams you an answer to say — live.

A desktop app for Windows that captures your **system audio** (what plays through your
speakers), transcribes speech **locally on the GPU**, detects when the other person
finishes a question, and streams an AI-generated answer into a floating overlay window.

Built for live video calls (Meet, Zoom, interviews): the other person asks something,
and a few seconds later you have a ready-to-say answer floating on your screen.

---

## 🎬 Demo

<!-- TODO: record a 15-20s GIF of the app catching a question and answering, save it to assets/demo.gif -->
<!-- Then uncomment the line below: -->
<!-- ![Demo](assets/demo.gif) -->

*Demo coming soon.*

---

## ✨ Why it's interesting (technically)

This isn't a wrapper around a chat API. The hard parts are all client-side and real-time:

- **System-audio capture** via Windows WASAPI **loopback** — it hears what your speakers
  play, not your microphone. No virtual cables needed.
- **Local GPU transcription** with `faster-whisper` (CUDA) — private, free, no audio
  ever leaves your machine for transcription.
- **Endpointing** — Voice Activity Detection figures out *when a question ends* (by the
  pause), so the AI fires at the right moment instead of on every word.
- **Streaming answers** — the response appears token by token, in real time.
- **Threaded architecture** — audio capture and inference run off the UI thread so the
  overlay never freezes.
- **Screen-share stealth** (optional) — `SetWindowDisplayAffinity` hides the window from
  screen captures while keeping it visible to you.

## 🧠 How it works

```
┌──────────────┐   ┌──────────────┐   ┌──────────────┐   ┌──────────────┐
│   Capture    │ → │  Transcribe  │ → │  Endpointing │ → │    Answer    │
│ WASAPI       │   │ faster-      │   │ webrtcvad    │   │ Gemini       │
│ loopback     │   │ whisper (GPU)│   │ (silence =   │   │ (streaming)  │
│ 16kHz mono   │   │              │   │  end of Q)   │   │              │
└──────────────┘   └──────────────┘   └──────────────┘   └──────────────┘
                                                                  ↓
                                                       ┌──────────────────┐
                                                       │  Floating overlay │
                                                       │  (PySide6)        │
                                                       └──────────────────┘
```

## 🛠️ Tech stack

| Layer | Tool |
|-------|------|
| Audio capture | `PyAudioWPatch` (WASAPI loopback) |
| Transcription | `faster-whisper` on CUDA, CPU fallback |
| Endpointing | `webrtcvad` |
| AI brain | Google Gemini (`google-genai`) |
| UI | `PySide6` (always-on-top frameless overlay) |
| Language | Python 3.12 |

## 📋 Requirements

- Windows 10/11
- Python 3.12
- NVIDIA GPU with CUDA recommended (works on CPU, slower)
- A free Gemini API key from [Google AI Studio](https://aistudio.google.com)

## 🚀 Setup

```bash
# 1. Create and activate a virtual environment
py -3.12 -m venv .venv
.venv\Scripts\activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. (GPU only) install the CUDA runtime libraries
pip install nvidia-cublas-cu12 nvidia-cudnn-cu12
```

Create a `.env` file in the project root with your own API key:

```
GEMINI_API_KEY=your_key_here
```

## ▶️ Run

```bash
python -m src.main
```

Or double-click `run.bat`.

Type the meeting context (topic + how you want to answer) in the box, click **Escuchar**,
and the copilot starts listening.

## ⚙️ Configuration

All settings live in [`src/config.py`](src/config.py):

| Setting | What it does |
|---------|--------------|
| `GEMINI_MODEL` | Which Gemini model to use |
| `WHISPER_MODEL` | Whisper size: `tiny`/`base`/`small`/`medium`/`large` |
| `WHISPER_LANGUAGE` | `"en"`, `"es"`, … or `None` to auto-detect |
| `SILENCE_MS_TO_ENDPOINT` | How long a pause counts as "question ended" |

The screen-share invisibility switch (`HIDE_FROM_SCREENSHARE`) is in
[`src/main.py`](src/main.py).

## 🗺️ Roadmap

- [ ] Demo GIF / video in this README
- [ ] Packaged `.exe` (PyInstaller) on a GitHub Release — run with no Python setup
- [ ] In-app language selector (English / Spanish)
- [ ] Conversation memory across the call
- [ ] Multiple saved context profiles

## 📝 Notes

- Each detected question = 1 API request. Gemini's free tier resets daily.
- Audio is transcribed **locally**; only the transcribed text is sent to Gemini.
- This is a client-side desktop app: it must run on your machine to hear your audio.
