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
    # Classic German Whisper hallucinations on silence / applause
    r"(?i)untertitel (im auftrag|der amara|von|by)",
    r"(?i)amara\.org",
    r"(?i)^vielen dank f(ü|ue)r'?s? (zuschauen|zuhören)",
    r"(?i)^(tschüss|danke)[\.!\s]*$",
]

HALLUCINATION_RE = [re.compile(p) for p in HALLUCINATION_PATTERNS]

# For lectures only drop whole-segment junk — a lecturer may well say
# "copyright" or "see you next week", which dictation filters out.
LECTURE_HALLUCINATION_PATTERNS = [
    r"^[\s.,!?\-;:]+$",
    r"(?i)^(you|music|\[.*\]|\(.*\)|\*.*\*)$",
    r"(?i)thanks for watching",
    r"(?i)^(please )?(like and )?subscribe",
    r"(?i)untertitel (im auftrag|der amara|von|by)",
    r"(?i)amara\.org",
    r"(?i)^vielen dank f(ü|ue)r'?s? (zuschauen|zuhören)",
    r"(?i)^thank you( very much| so much)?[.!]?$",   # Whisper's filler for silence
]
LECTURE_HALLUCINATION_RE = [re.compile(p) for p in LECTURE_HALLUCINATION_PATTERNS]


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


def clean_segment(text: str) -> str:
    """Lighter clean-up for long-form (lecture) segments — keeps punctuation."""
    if not text:
        return ""
    text = text.strip()
    for pattern in LECTURE_HALLUCINATION_RE:
        if pattern.search(text):
            logger.debug(f"Filtered hallucination: '{text}'")
            return ""
    text = re.sub(r'(?i)\b(\w+(?:\s+\w+)?)(?:[\s,]+\1\b){2,}', r'\1', text)
    # Repetition loops: "Das ist so. Das ist so. Das ist so." → once
    text = re.sub(r'(?:^|(?<=[.!?…]\s))([^.!?…]{1,60}[.!?…])(?:\s*\1){2,}', r'\1', text)
    return re.sub(r'\s{2,}', ' ', text).strip()


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
    # Multilingual Whisper (German, English, …) — used by lecture mode
    "small": {
        "backend": "whisper",
        "model_path": "small",
        "multilingual": True,
    },
    "medium": {
        "backend": "whisper",
        "model_path": "medium",
        "multilingual": True,
    },
    "large-v3-turbo": {
        "backend": "whisper",
        "model_path": "deepdml/faster-whisper-large-v3-turbo-ct2",
        "multilingual": True,
        "lecture_beam_size": 1,   # greedy: as accurate here and 2–3× faster on CPU
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
    # Parakeet TDT 0.6B v3 (sherpa-onnx, NVIDIA — top accuracy)
    "parakeet-tdt-0.6b-v3": {
        "backend": "sherpa-nemo-transducer",
        "repo": "csukuangfj/sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8",
        "encoder": "encoder.int8.onnx",
        "decoder": "decoder.int8.onnx",
        "joiner": "joiner.int8.onnx",
        "tokens": "tokens.txt",
        "feature_dim": 128,
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

    def __init__(self, model_path: str, on_progress=None, multilingual: bool = False,
                 lecture_beam_size: int | None = None):
        from faster_whisper import WhisperModel

        if on_progress:
            on_progress(f"Loading {model_path}…")

        start = time.time()
        self.multilingual = multilingual
        self.lecture_beam_size = lecture_beam_size or config.LECTURE_BEAM_SIZE
        self.model = WhisperModel(
            model_path,
            device=config.WHISPER_DEVICE,
            compute_type=config.WHISPER_COMPUTE_TYPE,
            cpu_threads=config.MAX_INFERENCE_THREADS,
        )
        logger.info(f"Whisper loaded: {model_path} ({time.time() - start:.1f}s)")

    def transcribe(self, audio: np.ndarray) -> str:
        segments, info = self.model.transcribe(
            audio,
            language=None if self.multilingual else config.WHISPER_LANGUAGE,
            beam_size=config.WHISPER_BEAM_SIZE_FINAL,
            temperature=config.WHISPER_TEMPERATURE,
            condition_on_previous_text=config.WHISPER_CONDITION_ON_PREVIOUS,
            vad_filter=True,
            without_timestamps=True,
        )
        return " ".join(seg.text.strip() for seg in segments).strip()

    def transcribe_segments(self, audio: np.ndarray, language: str | None = None,
                            prompt: str | None = None):
        """Long-form transcription with timestamps → ([(start, end, text)], language)."""
        if not self.multilingual:
            language = config.WHISPER_LANGUAGE
        segments, info = self.model.transcribe(
            audio,
            language=language,
            initial_prompt=prompt or None,      # course name → subject vocabulary
            beam_size=self.lecture_beam_size,
            condition_on_previous_text=False,   # avoids repetition loops on long audio
            vad_filter=True,
            without_timestamps=False,
        )
        out = [(seg.start, seg.end, seg.text) for seg in segments]
        return out, info.language


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


class _SherpaNemoTransducerBackend:
    """Sherpa-ONNX NeMo Transducer backend (Parakeet TDT)."""

    def __init__(self, model_info: dict, on_progress=None):
        import sherpa_onnx

        if on_progress:
            on_progress(f"Downloading model…")

        model_dir = _get_sherpa_model_dir(model_info["repo"])
        decoder_path = os.path.join(model_dir, model_info["decoder"])

        # NeMo ONNX exports lack required metadata in the decoder file.
        # Patch it once so sherpa-onnx's from_transducer() can load it.
        self._patch_decoder_metadata(decoder_path)

        if on_progress:
            on_progress(f"Loading Parakeet TDT…")

        feature_dim = model_info.get("feature_dim", 80)
        start = time.time()
        self._recognizer = sherpa_onnx.OfflineRecognizer.from_transducer(
            encoder=os.path.join(model_dir, model_info["encoder"]),
            decoder=decoder_path,
            joiner=os.path.join(model_dir, model_info["joiner"]),
            tokens=os.path.join(model_dir, model_info["tokens"]),
            num_threads=config.MAX_INFERENCE_THREADS,
            sample_rate=config.SAMPLE_RATE,
            feature_dim=feature_dim,
            model_type="nemo_transducer",
            decoding_method="greedy_search",
            provider="cpu",
        )
        logger.info(f"Parakeet TDT loaded ({time.time() - start:.1f}s)")

    @staticmethod
    def _patch_decoder_metadata(decoder_path: str):
        """Add missing vocab_size/context_size to NeMo decoder ONNX metadata."""
        import onnxruntime as ort

        sess = ort.InferenceSession(decoder_path)
        meta = dict(sess.get_modelmeta().custom_metadata_map)
        del sess

        if "vocab_size" in meta and "context_size" in meta:
            return  # Already patched

        import onnx

        logger.info("Patching NeMo decoder metadata (one-time fix)…")
        model = onnx.load(decoder_path)
        existing = {p.key for p in model.metadata_props}

        # Read vocab_size from encoder (same directory)
        encoder_path = decoder_path.replace("decoder", "encoder")
        enc_sess = ort.InferenceSession(encoder_path)
        enc_meta = dict(enc_sess.get_modelmeta().custom_metadata_map)
        del enc_sess
        vocab_size = enc_meta.get("vocab_size", "1024")

        if "vocab_size" not in existing:
            entry = model.metadata_props.add()
            entry.key = "vocab_size"
            entry.value = vocab_size

        if "context_size" not in existing:
            entry = model.metadata_props.add()
            entry.key = "context_size"
            entry.value = "2"

        onnx.save(model, decoder_path)
        logger.info(f"Decoder metadata patched: vocab_size={vocab_size}, context_size=2")

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

    def __init__(self, model_id: str | None = None):
        self._backend = None
        self._model_id: str = model_id or config.WHISPER_MODEL_FINAL
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
            self._backend = _WhisperBackend(
                info["model_path"], on_progress, multilingual=info.get("multilingual", False),
                lecture_beam_size=info.get("lecture_beam_size"),
            )
        elif backend_type == "moonshine":
            self._backend = _MoonshineBackend(info["model_path"], on_progress)
        elif backend_type == "sherpa-nemo-ctc":
            self._backend = _SherpaNemoCTCBackend(info, on_progress)
        elif backend_type == "sherpa-nemo-transducer":
            self._backend = _SherpaNemoTransducerBackend(info, on_progress)
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

    def transcribe_segments(self, audio: np.ndarray, language: str | None = None,
                            prompt: str | None = None):
        """
        Transcribe a long chunk with segment timestamps (lecture mode).

        language: "de", "en", … or None/"auto" to auto-detect.
        prompt: context such as the course name, to help with subject terms.
        Returns ([(start_s, end_s, text)], detected_language).
        """
        if self._backend is None:
            raise RuntimeError("Transcriber not loaded.")
        if not hasattr(self._backend, "transcribe_segments"):
            raise RuntimeError(
                f"Model '{self._model_id}' can't do timestamped transcription — "
                "choose a Whisper model for lecture mode."
            )
        if language in (None, "", "auto"):
            language = None

        start = time.time()
        segments, detected = self._backend.transcribe_segments(audio, language, prompt)
        cleaned = []
        for s, e, text in segments:
            text = clean_segment(text) if config.CLEAN_HALLUCINATIONS else text.strip()
            if text:
                cleaned.append((s, e, text))
        logger.info(
            f"[{self._model_id}] lecture chunk: {len(audio)/config.SAMPLE_RATE:.1f}s audio "
            f"in {time.time() - start:.1f}s, {len(cleaned)} segments, lang={detected}"
        )
        return cleaned, detected
