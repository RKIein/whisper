"""
Model Benchmark — compare standard whisper vs distil-whisper.

Records a short audio clip from your mic, then transcribes it with all
available model pairs so you can compare speed and accuracy side by side.

Usage:
    python benchmark.py              # Record 5 seconds, compare all models
    python benchmark.py --duration 10  # Record 10 seconds
    python benchmark.py --file clip.wav  # Use existing audio file
"""

import argparse
import sys
import time

import numpy as np
import sounddevice as sd

import config

# We'll import these lazily to show loading progress
WhisperModel = None


def record_audio(duration: float, sample_rate: int = 16000) -> np.ndarray:
    """Record audio from the default microphone."""
    print(f"\n  Recording for {duration} seconds — speak now!")
    print("  ", end="", flush=True)

    audio = sd.rec(
        int(duration * sample_rate),
        samplerate=sample_rate,
        channels=1,
        dtype="float32",
    )

    # Simple countdown
    for i in range(int(duration)):
        time.sleep(1)
        print("█", end="", flush=True)
    sd.wait()
    print(" Done!\n")

    return audio.flatten()


def load_audio_file(path: str, sample_rate: int = 16000) -> np.ndarray:
    """Load audio from a WAV file."""
    import wave

    with wave.open(path, "rb") as wf:
        frames = wf.readframes(wf.getnframes())
        audio = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0

        # Resample if needed
        if wf.getframerate() != sample_rate:
            from scipy.signal import resample

            target_len = int(len(audio) * sample_rate / wf.getframerate())
            audio = resample(audio, target_len)

    return audio


def benchmark_model(model_name: str, audio: np.ndarray, beam_size: int, language: str) -> dict:
    """Load a model, transcribe, return results."""
    from faster_whisper import WhisperModel

    # Load
    print(f"  Loading {model_name}...", end=" ", flush=True)
    t0 = time.time()
    model = WhisperModel(
        model_name,
        device=config.WHISPER_DEVICE,
        compute_type=config.WHISPER_COMPUTE_TYPE,
    )
    load_time = time.time() - t0
    print(f"({load_time:.1f}s)")

    # Transcribe
    print(f"  Transcribing...", end=" ", flush=True)
    t0 = time.time()
    segments, info = model.transcribe(
        audio,
        language=language,
        beam_size=beam_size,
        temperature=0.0,
        condition_on_previous_text=False,
        vad_filter=False,
        without_timestamps=True,
    )
    text = " ".join(seg.text.strip() for seg in segments).strip()
    transcribe_time = time.time() - t0
    print(f"({transcribe_time:.2f}s)")

    # Measure RAM (optional — needs psutil)
    try:
        import os
        import psutil
        process = psutil.Process(os.getpid())
        ram_mb = process.memory_info().rss / 1024 / 1024
    except ImportError:
        ram_mb = 0

    # Free the model
    del model

    return {
        "model": model_name,
        "beam_size": beam_size,
        "load_time": load_time,
        "transcribe_time": transcribe_time,
        "text": text,
        "ram_mb": ram_mb,
    }


def print_results(results: list[dict], audio_duration: float):
    """Print comparison table."""
    print("\n" + "═" * 72)
    print("  MODEL COMPARISON")
    print("═" * 72)

    for r in results:
        rtf = r["transcribe_time"] / audio_duration
        print(f"\n  Model:      {r['model']}")
        print(f"  Beam size:  {r['beam_size']}")
        print(f"  Load time:  {r['load_time']:.1f}s")
        print(f"  Speed:      {r['transcribe_time']:.2f}s ({rtf:.2f}x realtime)")
        if r['ram_mb'] > 0:
            print(f"  RAM (peak): {r['ram_mb']:.0f} MB")
        print(f"  Output:     {r['text']}")
        print("  " + "─" * 68)

    # Side-by-side summary
    print(f"\n  SUMMARY ({audio_duration:.1f}s of audio)")
    print("  " + "─" * 68)
    print(f"  {'Model':<24} {'Speed':>8} {'RTF':>8}  Text preview")
    print("  " + "─" * 68)
    for r in results:
        rtf = r["transcribe_time"] / audio_duration
        preview = r["text"][:40] + "…" if len(r["text"]) > 40 else r["text"]
        print(f"  {r['model']:<24} {r['transcribe_time']:>6.2f}s {rtf:>7.2f}x  {preview}")
    print()


def main():
    parser = argparse.ArgumentParser(description="Benchmark whisper models")
    parser.add_argument("--duration", type=float, default=5, help="Recording duration in seconds")
    parser.add_argument("--file", type=str, help="Use existing WAV file instead of recording")
    parser.add_argument("--language", type=str, default="en", help="Language code (en or de)")
    args = parser.parse_args()

    print("\n╔══════════════════════════════════════════╗")
    print("║   Whisper Model Benchmark                ║")
    print("╚══════════════════════════════════════════╝")

    # Get audio
    if args.file:
        print(f"\n  Loading audio from: {args.file}")
        audio = load_audio_file(args.file)
        audio_duration = len(audio) / 16000
        print(f"  Duration: {audio_duration:.1f}s")
    else:
        audio_duration = args.duration
        audio = record_audio(audio_duration)

    # Define model pairs to test
    models = [
        # Current setup
        {"name": "tiny", "beam": 1, "role": "interim (current)"},
        {"name": "small", "beam": 5, "role": "final (current)"},
        # Distil-whisper
        {"name": "distil-small.en", "beam": 1, "role": "interim (distil)"},
        {"name": "distil-medium.en", "beam": 5, "role": "final (distil)"},
    ]

    if args.language != "en":
        # Skip distil models for non-English
        models = [m for m in models if ".en" not in m["name"]]
        print(f"\n  Language: {args.language} — skipping English-only distil models")

    results = []
    for m in models:
        print(f"\n  ── {m['role']} ({m['name']}) ──")
        try:
            result = benchmark_model(m["name"], audio, m["beam"], args.language)
            result["role"] = m["role"]
            results.append(result)
        except Exception as e:
            print(f"  ERROR: {e}")
            print(f"  (Model may need to be downloaded first — run once with internet)")

    print_results(results, audio_duration)

    print("  Tip: Run this a few times to get consistent speed numbers.")
    print("  First run downloads models — subsequent runs use cache.\n")


if __name__ == "__main__":
    main()
