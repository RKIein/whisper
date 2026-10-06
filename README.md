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
- **Lecture mode** — record whole lectures and seminars, transcribe them live (German & English), and file them by course with timestamps, bookmarks and search
- **Timetable aware** — import your calendar (.ics, e.g. TraiNex) and each recording is tagged with course, Vorlesung/Seminar, room and lecturer automatically

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

**Updating:** double-click `Update.bat`. It stops the app, pulls the latest version, installs any new dependencies and starts it again. Your settings and lectures are kept.

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

## Lecture Mode

Record a full lecture or seminar, get a timestamped transcript, and have it filed under the right course — all offline.

**Start:** right-click the tray icon → **Start lecture…** (choose course, title, language), or, with a timetable set up, **● Record now: M13 Seminar · R1.0** for one-click start.

While recording (red icon):

| Action | How |
|---|---|
| Bookmark an important moment | `Ctrl+Shift+B` or tray → Add bookmark |
| Pause / resume (e.g. during the break) | Tray → Pause lecture |
| Stop | Tray → Stop lecture |

The audio is transcribed in ~60‑second chunks in the background (cut at pauses so no words are split), so the transcript is nearly finished when the lecture ends. After you stop, the icon turns amber until the last chunk is done, then a notification says the lecture is saved.

### Where lectures go

```
Documents/Lectures/
  M13 I Multivariate Verfahren, …/
    2026-10-06 13-45 – Seminar 2/
      audio.mp3          the recording (64 kbps mono)
      transcript.md      Markdown with [HH:MM:SS] timestamps + bookmarks
      transcript.srt     subtitles — open audio.mp3 in VLC to read along
      lecture.json       metadata (course, lecturer, room, language, …)
```

`transcript.md` starts with YAML front matter (course, date, duration, lecturer, room, tags), so the folder works directly as an **Obsidian** vault or in VS Code.

**Lecture library** (tray → Lecture library) shows courses → sessions → transcript, searches across *all* transcripts, and has buttons to play the audio, open the folder, rename or move a session, and **Transcribe again** (e.g. with a bigger model).

### Timetable / calendar

Tray → Lecture settings → **Calendar / timetable…**

- **Import .ics file** — e.g. the TraiNex *Studienplan* export, or
- **Subscription URL** — TraiNex calendar link, or Google/Outlook “secret address in iCal format”. Refreshed every 6 hours; the cached copy is used when offline.

When you start a recording up to 20 minutes before or during an event, the lecture is named and filed from the calendar: course (e.g. `M4 Dokumentation und Qualitätssicherung …`), type (`Vorlesung 3`, `Seminar 2`), room and lecturer. If you’re still recording 15 minutes after the scheduled end, you get a reminder.

### Lecture models

Tray → Lecture settings → Model. Lecture mode uses multilingual Whisper models (the dictation models are English-only):

| Model | Size | Notes |
|---|---|---|
| `small` (default) | ~480 MB | Keeps up live on most laptops |
| `medium` | ~1.5 GB | More accurate, needs a fast CPU to keep up live |
| `large-v3-turbo` | ~1.6 GB | Most accurate; if it can’t keep up, the backlog finishes after the lecture |

Pick the lecture’s language (Deutsch / English) for each course — it’s remembered. *Auto-detect* also works, but a fixed language is more reliable for lectures full of technical terms.

### If something goes wrong

The audio is written to disk continuously. If the app or laptop crashes mid-lecture, the recording up to that moment is kept; on the next start you get a notification, and **Lecture library → Transcribe again** finishes the transcript. You can also run it by hand:

```bash
python lecture.py --transcribe "Documents/Lectures/<Course>/<Session>"
```

> Recording lectures may need the lecturer’s permission — check your university’s rules.

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

Everything runs locally. No audio, text, or data of any kind is sent anywhere. The only optional exception is downloading your timetable, if you set a calendar subscription URL. The mic is only active while the green icon is showing.

---

## License

MIT — see [LICENSE](LICENSE)
