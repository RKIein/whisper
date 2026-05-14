"""
Transcriber — multi-backend speech-to-text with hot-swapping.

Supports three backends:
  - faster-whisper (CTranslate2 INT8) for Whisper & Distil-Whisper models
  - moonshine_onnx for Moonshine models (Useful Sensors)
  - sherpa-onnx for Parakeet TDT and SenseVoice models

All models are lazy-loaded and can be switched at runtime.
"""

import logging
import os
import re
import time
import threading

import numpy as np

import config

logger = logging.getLogger(__name__)


# ─── Hallucination filter ────────────────────────────────────

HALLUCINATION_PATTERNS = [
    r"^[\s.,!?\-;:]+$",
    r"(?i)^thank(s| you)[\.\s]*$",
    r"(?i)^bye[\.\s]*$",
    r"(?i)^okay[\.\s]*$",
    r"(?i)^so[\.\s]*$",
    r"(?i)^you$",
    r"(?i)^the end[\.\s]*$",
    r"(?i)^thanks for watching",
    r"(?i)^(please )?subscribe",
    r"(?i)^like and subscribe",
    r"(?i)^see you (next|in the)",
    r"(?i)^copyright",
    r"(?i)^music$",
    r"(?i)^\[.*\]$",
]

HALLUCINATION_RE = [re.compile(p) for p in HALLUCINATION_PATTERNS]


def clean_transcription(text: str) -> str:
    if not text:
        return ""
    text = text.strip()
    for pattern in HALLUCINATION_RE:
        if pattern.match(text):
            logger.debug(f"Filtered hallucination: '{text}'")
            return ""
    text = re.sub(r'\b(\w+(?:\s+\w+)?)\s+(?:\1\s*){2,}', r'\1', text)
    text = re.sub(r'^[\s,.\-!?;:]+', '', text)
    text = re.sub(r'[\s,.\-]+$', '', text)
    text = re.sub(r'\s{2,}', ' ', text)
    if len(text.strip()) < 2:
        return ""
    return text.strip()


# ─── Model registry ─────────────────────────────────────────

# backend: "whisper" | "moonshine" | "sherpa-transducer" | "sherpa-sensevoice"
MODEL_REGISTRY = {
    # Whisper (faster-whisper / CTranslate2)
    "base.en": {
        "backend": "whisper",
        "model_path": "base.en",
    },
    "small.en": {
        "backend": "whisper",
        "model_path": "small.en",
    },
    "medium.en": {
        "backend": "whisper",
        "model_path": "medium.en",
    },
    # Distil-Whisper (faster-whisper compatible)
    "distil-small.en": {
        "backend": "whisper",
        "model_path": "Systran/faster-distil-whisper-small.en",
    },
    "distil-medium.en": {
        "backend": "whisper",
        "model_path": "Systran/faster-distil-whisper-medium.en",
    },
    # Moonshine (ONNX)
    "moonshine-tiny": {
        "backend": "moonshine",
        "model_path": "moonshine/tiny",
    },
    "moonshine-base": {
        "backend": "moonshine",
        "model_path": "moonshine/base",
    },
    # Parakeet CTC 110M (sherpa-onnx, NVIDIA)
    "parakeet-ctc-110m": {
        "backend": "sherpa-nemo-ctc",
        "repo": "csukuangfj/sherpa-onnx-nemo-parakeet_tdt_ctc_110m-en-36000",
        "model": "model.onnx",
        "tokens": "tokens.txt",
    },
    # SenseVoice (sherpa-onnx)
    "sensevoice-small": {
        "backend": "sherpa-sensevoice",
        "repo": "csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17",
        "model": "model.int8.onnx",
        "tokens": "tokens.txt",
    },
}


def _get_sherpa_model_dir(repo: str) -> str:
    """Get or download a sherpa-onnx model from HuggingFace."""
    cache_dir = os.path.join(os.path.expanduser("~"), ".cache", "sherpa-onnx")
    model_dir = os.path.join(cache_dir, repo.split("/")[-1])

    if os.path.exists(model_dir):
        return model_dir

    os.makedirs(cache_dir, exist_ok=True)
    logger.info(f"Downloading {repo}...")

    from huggingface_hub import snapshot_download
    model_dir = snapshot_download(
        repo_id=repo,
        local_dir=model_dir,
    )
    return model_dir


# ─── Backend wrappers ────────────────────────────────────────

class _WhisperBackend:
    """faster-whisper / CTranslate2 backend."""

    def __init__(self, model_path: str, on_progress=None):
        from faster_whisper import WhisperModel

        if on_progress:
            on_progress(f"Loading {model_path}…")

        start = time.time()
        self.model = WhisperModel(
            model_path,
            device=config.WHISPER_DEVICE,
            compute_type=config.WHISPER_COMPUTE_TYPE,
        )
        logger.info(f"Whisper loaded: {model_path} ({time.time() - start:.1f}s)")

    def transcribe(self, audio: np.ndarray) -> str:
        segments, info = self.model.transcribe(
            audio,
            language=config.WHISPER_LANGUAGE,
            beam_size=config.WHISPER_BEAM_SIZE_FINAL,
            temperature=config.WHISPER_TEMPERATURE,
            condition_on_previous_text=config.WHISPER_CONDITION_ON_PREVIOUS,
            vad_filter=True,
            without_timestamps=True,
        )
        return " ".join(seg.text.strip() for seg in segments).strip()


class _MoonshineBackend:
    """Moonshine ONNX backend."""

    def __init__(self, model_path: str, on_progress=None):
        import moonshine_onnx

        if on_progress:
            on_progress(f"Loading {model_path}…")

        start = time.time()
        self._model = moonshine_onnx.MoonshineOnnxModel(model_name=model_path)
        self._tokenizer = moonshine_onnx.load_tokenizer()
        logger.info(f"Moonshine loaded: {model_path} ({time.time() - start:.1f}s)")

    def transcribe(self, audio: np.ndarray) -> str:
        # Moonshine expects float32 audio at 16kHz, shape (1, samples)
        if audio.dtype != np.float32:
            audio = audio.astype(np.float32)
        if audio.ndim > 1:
            audio = audio.flatten()
        logger.debug(
            f"Moonshine input: shape={audio.shape}, "
            f"min={audio.min():.4f}, max={audio.max():.4f}, "
            f"rms={np.sqrt(np.mean(audio**2)):.6f}"
        )
        audio_2d = audio[np.newaxis, :]
        tokens = self._model.generate(audio_2d)
        text = self._tokenizer.decode_batch(tokens)
        logger.debug(f"Moonshine raw output: tokens={tokens}, text={repr(text)}")
        if isinstance(text, list):
            return " ".join(text).strip()
        return str(text).strip()


class _SherpaNemoCTCBackend:
    """Sherpa-ONNX NeMo CTC backend (Parakeet CTC)."""

    def __init__(self, model_info: dict, on_progress=None):
        import sherpa_onnx

        if on_progress:
            on_progress(f"Downloading model…")

        model_dir = _get_sherpa_model_dir(model_info["repo"])

        if on_progress:
            on_progress(f"Loading Parakeet CTC…")

        start = time.time()
        self._recognizer = sherpa_onnx.OfflineRecognizer.from_nemo_ctc(
            model=os.path.join(model_dir, model_info["model"]),
            tokens=os.path.join(model_dir, model_info["tokens"]),
            num_threads=config.MAX_INFERENCE_THREADS,
            sample_rate=config.SAMPLE_RATE,
            decoding_method="greedy_search",
            provider="cpu",
        )
        logger.info(f"Parakeet CTC loaded ({time.time() - start:.1f}s)")

    def transcribe(self, audio: np.ndarray) -> str:
        if audio.dtype != np.float32:
            audio = audio.astype(np.float32)
        if audio.ndim > 1:
            audio = audio.flatten()

        stream = self._recognizer.create_stream()
        stream.accept_waveform(config.SAMPLE_RATE, audio)
        self._recognizer.decode_stream(stream)
        return stream.result.text.strip()


class _SherpaSenseVoiceBackend:
    """Sherpa-ONNX SenseVoice backend."""

    def __init__(self, model_info: dict, on_progress=None):
        import sherpa_onnx

        if on_progress:
            on_progress(f"Downloading model…")

        model_dir = _get_sherpa_model_dir(model_info["repo"])

        if on_progress:
            on_progress(f"Loading SenseVoice…")

        start = time.time()
        self._recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=os.path.join(model_dir, model_info["model"]),
            tokens=os.path.join(model_dir, model_info["tokens"]),
            num_threads=config.MAX_INFERENCE_THREADS,
            sample_rate=config.SAMPLE_RATE,
            use_itn=True,
            language="en",
            provider="cpu",
        )
        logger.info(f"SenseVoice loaded ({time.time() - start:.1f}s)")

    def transcribe(self, audio: np.ndarray) -> str:
        if audio.dtype != np.float32:
            audio = audio.astype(np.float32)
        if audio.ndim > 1:
            audio = audio.flatten()

        stream = self._recognizer.create_stream()
        stream.accept_waveform(config.SAMPLE_RATE, audio)
        self._recognizer.decode_stream(stream)
        return stream.result.text.strip()


# ─── Main Transcriber ────────────────────────────────────────

class Transcriber:
    """
    Multi-backend transcriber with hot-swapping.
    Supports Whisper, Distil-Whisper, Moonshine, Parakeet, and SenseVoice.
    """

    def __init__(self):
        self._backend = None
        self._model_id: str = config.WHISPER_MODEL_FINAL
        self._loaded = False
        self._load_lock = threading.Lock()

    @property
    def is_loaded(self) -> bool:
        return self._loaded

    @property
    def model_id(self) -> str:
        return self._model_id

    def load(self, on_progress=None):
        with self._load_lock:
            if self._loaded:
                return
            self._load_model(self._model_id, on_progress)
            self._loaded = True

    def switch_model(self, model_id: str, on_progress=None):
        """Switch to a different model. Blocks while loading."""
        with self._load_lock:
            if model_id == self._model_id and self._backend is not None:
                return

            logger.info(f"Switching model: {self._model_id} → {model_id}")
            self._backend = None
            self._model_id = model_id
            self._load_model(model_id, on_progress)
            self._loaded = True

    def _load_model(self, model_id: str, on_progress=None):
        info = MODEL_REGISTRY.get(model_id)
        if info is None:
            raise ValueError(f"Unknown model: {model_id}")

        backend_type = info["backend"]

        if backend_type == "whisper":
            self._backend = _WhisperBackend(info["model_path"], on_progress)
        elif backend_type == "moonshine":
            self._backend = _MoonshineBackend(info["model_path"], on_progress)
        elif backend_type == "sherpa-nemo-ctc":
            self._backend = _SherpaNemoCTCBackend(info, on_progress)
        elif backend_type == "sherpa-sensevoice":
            self._backend = _SherpaSenseVoiceBackend(info, on_progress)
        else:
            raise ValueError(f"Unknown backend: {backend_type}")

    def transcribe(self, audio: np.ndarray) -> str:
        """Transcribe audio using the active backend."""
        if self._backend is None:
            raise RuntimeError("Transcriber not loaded.")

        start = time.time()
        text = self._backend.transcribe(audio)
        elapsed = time.time() - start

        if config.CLEAN_HALLUCINATIONS and text:
            text = clean_transcription(text)

        if text:
            logger.info(
                f"[{self._model_id}] ({elapsed:.2f}s, "
                f"{len(audio)/config.SAMPLE_RATE:.1f}s audio) {text}"
            )

        return text
