# 🎧 Copiloto de Llamada

> Real-time AI copilot that listens to your call, understands the questions, and streams you an answer to say — live.

A desktop app for Windows that captures your **system audio** (what plays through your
speakers), transcribes speech **locally on the GPU**, detects when the other person
finishes a question, and streams an AI-generated answer into a floating overlay window.

Built for live video calls (Meet, Zoom, interviews): the other person asks something,
and a few seconds later you have a ready-to-say answer floating on your screen — in
English to read out loud, with a Spanish translation right beside it so you understand it.

You pick exactly **which audio device** to listen to, so it works even with virtual
mixers (Voicemeeter, VB-Cable) or multiple outputs.

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
- **Offline bilingual output** — the finished answer is translated to Spanish locally
  with `argostranslate` (runs on the same CTranslate2 engine as Whisper). No API, no
  Gemini tokens, no rate limits — it never fails mid-call.
- **Selectable audio source** — a device dropdown lets you capture any output loopback,
  not just the system default, and your choice is remembered between runs.
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
                                                       │  Translate (ES)   │
                                                       │  argostranslate   │
                                                       │  offline, local   │
                                                       └──────────────────┘
                                                                  ↓
                                                       ┌──────────────────────────┐
                                                       │  Floating overlay (PySide6)│
                                                       │  EN answer │ ES translation │
                                                       └──────────────────────────┘
```

## 🛠️ Tech stack

| Layer | Tool |
|-------|------|
| Audio capture | `PyAudioWPatch` (WASAPI loopback, selectable device) |
| Transcription | `faster-whisper` on CUDA, CPU fallback |
| Endpointing | `webrtcvad` |
| AI brain | Google Gemini (`google-genai`) |
| Translation | `argostranslate` (offline EN→ES, no tokens) |
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
```

> ⚠️ **GPU users — don't skip this.** `faster-whisper` needs the CUDA runtime
> libraries, which are **not** in `requirements.txt` (they're ~1.2 GB and useless on
> CPU-only machines). If you have an NVIDIA GPU, install them too:
>
> ```bash
> pip install nvidia-cublas-cu12 nvidia-cudnn-cu12
> ```
>
> Skip this and transcription crashes with `Library cublas64_12.dll is not found`
> (see [Troubleshooting](#-troubleshooting)). On a CPU-only machine you can skip it —
> the app falls back to CPU automatically (slower).

Create a `.env` file in the project root with your own API key:

```
GEMINI_API_KEY=your_key_here
```

> ℹ️ On first launch the offline translator downloads its English→Spanish model
> (~100 MB) once. That single download needs internet; every translation afterward
> is fully offline.

## ▶️ Run

```bash
python -m src.main
```

Or double-click `run.bat`.

Type the meeting context (topic + how you want to answer) in the box, pick your
**audio source** from the dropdown (the device you actually hear the call through —
use **⟳** to refresh the list), then click **Escuchar**.

When the other person finishes a question, the English answer streams into the left
panel and its Spanish translation appears on the right once the answer completes.

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

## 🩺 Troubleshooting

| Symptom | Cause & fix |
|---------|-------------|
| `Library cublas64_12.dll is not found or cannot be loaded` | The CUDA runtime libs aren't installed. Run `pip install nvidia-cublas-cu12 nvidia-cudnn-cu12` (step 3 above). |
| Status says *Escuchando...* but nothing transcribes | The selected audio source has no sound. Pick the device you actually hear the call through (not a silent/virtual output), and make sure audio is playing. |
| `py -3.12` → *No suitable Python runtime found* | Python 3.12 isn't installed. Install it (`winget install Python.Python.3.12`) or use the `py` launcher to target a 3.12 you have. |
| Transcription is wrong or empty | `WHISPER_LANGUAGE` in [`src/config.py`](src/config.py) is locked to `"en"`. Set it to your call's language (e.g. `"es"`) or `None` to auto-detect. |

## 🗺️ Roadmap

- [x] Offline Spanish translation beside the English answer
- [x] In-app audio source selector (remembers your choice)
- [ ] Demo GIF / video in this README
- [ ] Packaged `.exe` (PyInstaller) on a GitHub Release — run with no Python setup
- [ ] Selectable translation target language (beyond Spanish)
- [ ] Conversation memory across the call
- [ ] Multiple saved context profiles

## 📝 Notes

- Each detected question = 1 API request. Gemini's free tier resets daily.
- Audio is transcribed **locally**; only the transcribed text is sent to Gemini.
- This is a client-side desktop app: it must run on your machine to hear your audio.
