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
