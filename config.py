"""
Configuration for the Whisper Dictation App.
"""

# --- Audio Settings ---
SAMPLE_RATE = 16000
CHANNELS = 1
DTYPE = "float32"
BLOCK_DURATION_MS = 32
BLOCK_SIZE = 512

# --- VAD Settings ---
VAD_THRESHOLD = 0.5
SILENCE_DURATION_S = 0.8
MIN_SPEECH_DURATION_S = 0.3
MAX_SPEECH_DURATION_S = 30

# --- Model (English only, faster-whisper + CTranslate2/oneMKL) ---
#
# Single model: base.en — good accuracy, fast enough for batch on i5.
# No preview model — all CPU budget goes to accurate final transcription.
WHISPER_MODEL_FINAL = "base.en"
WHISPER_DEVICE = "cpu"
WHISPER_COMPUTE_TYPE = "int8"
WHISPER_LANGUAGE = "en"
WHISPER_BEAM_SIZE_FINAL = 5
WHISPER_TEMPERATURE = 0.0
WHISPER_CONDITION_ON_PREVIOUS = False

# --- Rolling Batch Settings ---
BATCH_INTERVAL_S = 60
BATCH_OVERLAP_S = 3

# --- Post-processing ---
CLEAN_HALLUCINATIONS = True

# --- Hotkey Settings ---
HOTKEY_TOGGLE_DICTATION = "<ctrl>+<shift>+<space>"

# --- Text Injection Settings ---
INJECTION_METHOD = "clipboard"
CLIPBOARD_RESTORE = True
INJECT_TRAILING_SPACE = True

# --- CPU Optimization ---
MAX_INFERENCE_THREADS = 4         # Use 4 of your 8 physical cores
LOW_PRIORITY = True               # Run at below-normal priority

# --- Display / Feedback ---
LOG_TRANSCRIPTIONS = True
LOG_FILE = "whisper-history.log"
LOG_MAX_BYTES = 5 * 1024 * 1024  # 5 MB — rotate when exceeded

# --- Lecture Mode ---
LECTURE_CHUNK_S = 60              # Transcribe in ~60 s chunks while recording
LECTURE_CUT_SEARCH_S = 5          # Cut chunks at the quietest moment in the last 5 s
LECTURE_BEAM_SIZE = 5
LECTURE_PARAGRAPH_S = 30          # Group transcript into ~30 s paragraphs
LECTURE_MP3_BITRATE = 64          # kbps, mono speech
LECTURE_OVERRUN_REMINDER_MIN = 15 # Remind to stop if still recording after the calendar slot
HOTKEY_LECTURE_BOOKMARK = "<ctrl>+<shift>+b"

# Models offered for lecture mode (multilingual Whisper, timestamped)
LECTURE_MODELS = {
    "small":          {"label": "Whisper Small (multilingual)",  "size": "~480 MB"},
    "medium":         {"label": "Whisper Medium (multilingual)", "size": "~1.5 GB"},
    "large-v3-turbo": {"label": "Whisper Large-v3 Turbo",        "size": "~1.6 GB"},
}
