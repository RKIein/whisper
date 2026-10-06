# Whisper Dictation App — Setup Guide

## Quick Start

```bash
# 1. Create a virtual environment
cd C:\Users\Robin\Workspace\4_Projects\whisper
python -m venv venv
venv\Scripts\activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Run the app
python app.py
```

## First Run

On first run, the app will:
1. Download the selected speech model (~150 MB for base.en, varies by model)
2. Models are cached in `~/.cache/huggingface/hub/`

This only happens once per model.

## System Tray

The app runs as a system tray application (notification area, bottom-right of the taskbar).

- **Left-click** the tray icon to stop dictation
- **Right-click** the tray icon for the full menu
- The icon changes color: gray = idle, blue = loading, green = listening, amber = transcribing

## Dictation

| Hotkey | Action |
|---|---|
| `Ctrl+Shift+Space` | Toggle dictation on/off (or hold in hold mode) |
| `Escape` | Cancel recording (discard without transcribing) |

### How it works

1. Press `Ctrl+Shift+Space` to start
2. Speak naturally — text is inserted where your cursor was
3. Press `Ctrl+Shift+Space` again to stop
4. The app transcribes and pastes the text via clipboard

## Voice Recording

Start/stop voice recording from the tray menu. Recordings are saved to
`~/Documents/Whisper Recordings/` in the selected format (WAV, MP3, or OGG).

MP3 works out of the box. OGG requires ffmpeg on PATH.

## Lecture Mode

Tray → **Start lecture…** records a whole lecture/seminar and transcribes it
live in ~60 s chunks with a multilingual Whisper model (German/English).
Lectures are saved to `~/Documents/Lectures/<Course>/<date – title>/`
(audio.mp3, transcript.md, transcript.srt, lecture.json).

- `Ctrl+Shift+B` — bookmark while recording
- Tray → Lecture library — browse, search, play, re-transcribe
- Tray → Lecture settings → Calendar / timetable… — import your .ics
  (e.g. TraiNex Studienplan) so course, type, room and lecturer are filled in
- Tray → Lecture settings → Model — small (default), medium, large-v3-turbo

Run the tests with `pip install pytest` and `python -m pytest tests`.

## Available Models

Switch models from the tray menu. Available backends:

- **Whisper** (faster-whisper): base.en, small.en, medium.en
- **Distil-Whisper**: distil-small.en, distil-medium.en
- **Moonshine** (ONNX): moonshine-tiny, moonshine-base
- **Whisper Large v3 Turbo** (faster-whisper): large-v3-turbo
- **Parakeet CTC** (sherpa-onnx): parakeet-ctc-110m
- **Parakeet TDT** (sherpa-onnx): parakeet-tdt-0.6b-v3
- **SenseVoice** (sherpa-onnx): sensevoice-small

## Configuration

Edit `config.py` to change:
- **VAD_THRESHOLD**: 0.5 default — raise if picking up background noise
- **SILENCE_DURATION_S**: 0.8s default — lower for faster response
- **HOTKEY_TOGGLE_DICTATION**: change the hotkey combo
- **MAX_INFERENCE_THREADS**: CPU threads for transcription (default 4)

User preferences (model, hotkey mode, sound, recording format) are saved
automatically in `settings.json`.

## Building a .exe

```bash
pip install pyinstaller
python build.py
```

Output: `dist/WhisperDictation/WhisperDictation.exe`
Models download on first run.

## Troubleshooting

**No text appearing?**
- Make sure your cursor is in a text field before starting dictation
- Check that no other app is using `Ctrl+Shift+Space`

**Picking up background noise?**
- Raise `VAD_THRESHOLD` in config.py (try 0.6 or 0.7)

**Tray icon not showing?**
- Settings > Personalization > Taskbar > System tray > show all icons
- Or click the ^ arrow in the notification area
