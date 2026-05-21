# Whisper Dictation

A local, offline speech-to-text dictation tool for Windows. Lives in your system tray, stays out of your way, and types wherever your cursor is.

Built because I wanted something fast and reliable that runs entirely on my CPU no API cost, no cloud, no subscription.

---

## What it does

- **One hotkey** (`Ctrl+Shift+Space`) starts and stops dictation
- **Types directly** into whatever app is in focus — browser, Word, Notepad, anything
- **Lives in the system tray** — icon changes color to show what it's doing
- **Runs fully offline** on CPU — tested on a laptop, no GPU needed
- **Switch models on the fly** from the tray menu to find the speed/accuracy balance you like
- **Voice recording** — optionally save recordings to your Documents folder (WAV or MP3)

---

## Screenshot

*Coming soon*

---

## Requirements

- Windows 10 or 11
- Python 3.10+

---

## Installation

```bash
# 1. Clone the repo
git clone https://github.com/RKIein/whisper.git
cd whisper

# 2. Create a virtual environment
python -m venv venv
venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Run
python app.py
```

The first time you run it, the app downloads the selected model (~150 MB for the default). This happens once and then it's cached.

---

## Usage

| Action | How |
|---|---|
| Start / stop dictation | `Ctrl+Shift+Space` |
| Cancel recording | `Escape` |
| Switch model / settings | Right-click the tray icon |
| Exit | Right-click tray → Exit |

The tray icon tells you what's happening:

| Color | State |
|---|---|
| Gray | Idle |
| Blue | Loading model |
| Green | Listening |
| Amber | Transcribing |

---

## Available Models

Switch models from the tray menu. All run locally on CPU:

| Model | Backend | Notes |
|---|---|---|
| `base.en` | faster-whisper | Good default, fast on most CPUs |
| `small.en` | faster-whisper | More accurate, a bit slower |
| `medium.en` | faster-whisper | Best accuracy, needs a decent CPU |
| `distil-small.en` | faster-whisper | Distilled, very fast |
| `distil-medium.en` | faster-whisper | Distilled, good balance |
| `moonshine-tiny` | sherpa-onnx | Tiny and quick |
| `moonshine-base` | sherpa-onnx | Good for short phrases |
| `parakeet-ctc-110m` | sherpa-onnx | NVIDIA model, very fast |
| `sensevoice-small` | sherpa-onnx | Multilingual |

If you're on a mid-range laptop, `parakeet-ctc-110m` or `distil-small.en` are good starting points.
I usually defaut to `parakeet-ctc-110m`

---

## Configuration

Edit `config.py` to tweak behaviour:

```python
VAD_THRESHOLD = 0.5        # Raise to 0.6–0.7 if picking up background noise
MAX_INFERENCE_THREADS = 4  # CPU threads for transcription
```

User preferences (model choice, hotkey mode, sound feedback) are saved automatically in `settings.json`.

---

## Build a standalone .exe

If you want to run it without Python installed:

```bash
pip install pyinstaller
python build.py
```

Output lands in `dist/WhisperDictation/WhisperDictation.exe`. Models still download on first run.

---

## Troubleshooting

**No text appearing after I speak**
Make sure your cursor is in a text field before starting dictation. The app types via clipboard, so focus matters.

**Picking up background noise or fan noise**
Raise `VAD_THRESHOLD` in `config.py` — try `0.6` or `0.7`.

**Tray icon not visible**
Go to Settings → Personalization → Taskbar → Other system tray icons and enable it there.

**Hotkey conflicts with another app**
Change `HOTKEY_TOGGLE_DICTATION` in `config.py` to a different combo.

---

## Privacy

Everything runs locally. No audio, text, or data of any kind is sent anywhere. The mic is only active while the green icon is showing.

---

## License

MIT — see [LICENSE](LICENSE)
