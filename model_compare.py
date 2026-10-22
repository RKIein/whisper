"""
Which speech model transcribes Robin's lectures best? A side-by-side test on Colab.

The same 5-minute clips from real GoodNotes recordings go through several models. No
reference transcript exists, so every output is scored on what can be counted:
  • odd words   — words German doesn't have (wordfreq) and that aren't on the slides:
                  mostly mishearings ("Datenerhöhung", "Skalengeduld"); lower = better
  • slide terms — technical terms from the lecture's slides that come out right
  • words/min   — far below the others = speech was lost
  • fillers     — invented "Vielen Dank." / "Untertitel …" lines
The texts are written to Drive as Version A, B, C … with the key in a separate file,
so they can be read without knowing which model wrote them.

Each model runs in its own process (and, where its packages clash, in its own venv),
so one failing install or crash doesn't take the others down.

Colab:  model_compare.prepare(...) → clips;  model_compare.run_all(...) → report.
Inside: python model_compare.py run <model> <manifest.json> <out.json>
"""

import json
import os
import random
import re
import string
import subprocess
import sys
import time
import wave
from datetime import datetime

SR = 16000
WORD = re.compile(r"[A-Za-zÄÖÜäöüß][A-Za-zÄÖÜäöüß-]*[A-Za-zÄÖÜäöüß]")
FILLER = re.compile(r"(?i)^(vielen dank( f(ü|ue)r'?s? (zuschauen|zuhören))?|untertitel.*|"
                    r"danke f(ü|ue)r'?s? zuschauen|bis zum nächsten mal)[.!]?$")

# name → (where it runs, what to install there, description)
MODELS = {
    "whisper-batched":    ("system", [], "Whisper large-v3, batched (current setup)"),
    "whisper-batched-loud": ("system", [], "Whisper large-v3, batched, quiet audio turned up"),
    "whisper-batched-loud-vad20": ("system", [], "Whisper large-v3, batched, louder, speech detector 0.20"),
    "whisper-batched-loud-vad05": ("system", [], "Whisper large-v3, batched, louder, speech detector 0.05"),
    "whisper-sequential": ("system", [], "Whisper large-v3, sequential with context"),
    "whisper-sequential-loud": ("system", [], "Whisper large-v3, sequential, quiet audio turned up"),
    "primeline-german":   ("system", [], "Whisper large-v3 German fine-tune (primeline)"),
    "primeline-turbo-de": ("system", [], "Whisper large-v3-turbo German fine-tune (primeline)"),
    "voxtral-mini":       ("voxtral", ["transformers>=4.54", "mistral-common[audio]>=1.8.1",
                                       "accelerate", "bitsandbytes"], "Voxtral Mini 3B (Mistral)"),
    "voxtral-small-4bit": ("voxtral", None, "Voxtral Small 24B, 4-bit (Mistral)"),
    "qwen3-asr":          ("qwen", ["qwen-asr"], "Qwen3-ASR 1.7B (Alibaba)"),
}


# ─── Clips (system Python, uses the production code to find recordings) ────

def _write_wav(path: str, audio) -> None:
    import numpy as np
    pcm = (np.clip(audio, -1, 1) * 32767).astype("<i2")
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())


def loudness(audio, step_s: int = 60) -> list[float]:
    """dBFS per minute: shows whether a recording goes (nearly) silent."""
    import numpy as np
    out = []
    for i in range(0, len(audio), step_s * SR):
        part = audio[i:i + step_s * SR]
        rms = float(np.sqrt(np.mean(part.astype("float64") ** 2))) if len(part) else 0.0
        out.append(round(float(20 * np.log10(rms)), 1) if rms > 0 else -120.0)
    return out


def louder(audio):
    """Quiet recordings up to normal speech level (what the Lecture Transcriber now does)."""
    from lecture_store import normalize_loudness
    return normalize_loudness(audio)


def _read_wav(path: str):
    import numpy as np
    with wave.open(path) as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype("float32") / 32768.0


def prepare(drive_root: str, clips: list[dict], out_dir: str, log=print) -> list[dict]:
    """
    Cut the clips. clips: [{"id", "notebook" (part of the GoodNotes name), "recording"
    (start of its id), "start_min", "minutes", "why"}]. Writes <id>.wav and returns the
    manifest, with slide text and the recording's loudness per minute.
    """
    import colab_batch as cb
    import lecture_store as store
    os.makedirs(out_dir, exist_ok=True)
    files = cb.backup_files(cb.find_goodnotes_backup(drive_root))
    manifest = []
    for c in clips:
        path = next((p for p in files if c["notebook"].lower() in os.path.basename(p).lower()), None)
        if not path:
            log(f"✗ {c['id']}: no GoodNotes notebook with “{c['notebook']}”")
            continue
        recs = cb.from_goodnotes(path, out_dir)
        rec = next((r for r in recs if c["recording"].lower() in r.source.lower()), None)
        if not rec:
            log(f"✗ {c['id']}: recording {c['recording']} not in {os.path.basename(path)}")
            continue
        audio = store.decode_audio(rec.open(), SR)
        a = int(c["start_min"] * 60 * SR)                 # minutes None: to the end
        b = int((c["start_min"] + c["minutes"]) * 60 * SR) if c.get("minutes") else len(audio)
        wav = os.path.join(out_dir, f"{c['id']}.wav")
        _write_wav(wav, audio[a:b])
        manifest.append(dict(c, wav=wav, seconds=round(min(b, len(audio)) / SR - a / SR, 1),
                             slides=cb.slide_text(path), loudness=loudness(audio)))
        log(f"✓ {c['id']}: {os.path.basename(path)} › {c['recording']}, "
            f"min {c['start_min']}–{min(b, len(audio)) / SR / 60:.0f} of {len(audio) / SR / 60:.0f}")
    with open(os.path.join(out_dir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False)
    return manifest


# ─── Running the models ──────────────────────────────────────

def _venv_python(name: str, packages: list[str], log) -> str:
    """
    A venv on top of Colab's packages, with `packages` overriding. pip (not uv) so Colab's
    torch counts as installed, and torch/torchvision/torchaudio are pinned to Colab's:
    round 2 failed because uv put a second torch into the venv that didn't match
    Colab's torchvision ("operator torchvision::nms does not exist").
    """
    from importlib import metadata
    root = f"/content/venvs/{name}"
    py = f"{root}/bin/python"
    if not os.path.exists(py):
        log(f"   setting up {name} environment (a few minutes)…")
        pins = os.path.join("/content/venvs", "torch-pins.txt")
        os.makedirs("/content/venvs", exist_ok=True)
        with open(pins, "w") as f:
            for dist in ("torch", "torchvision", "torchaudio"):
                try:
                    f.write(f"{dist}=={metadata.version(dist)}\n")
                except metadata.PackageNotFoundError:
                    pass
        for cmd in ([sys.executable, "-m", "venv", "--system-site-packages", root],
                    [py, "-m", "pip", "install", "-q", "-c", pins, *packages]):
            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode != 0:
                raise RuntimeError(f"{' '.join(cmd[:4])} failed:\n{(r.stdout + r.stderr)[-3000:]}")
    return py


def run_all(manifest_path: str, models: list[str], out_dir: str, log=print) -> dict:
    """
    Every model over every clip, each in its own process. → {model: {clip: result}}.
    A model that fails as a whole gets {"_error": last lines of its output} for the report.
    """
    results = {}
    for name in models:
        env, packages, label = MODELS[name]
        if packages is None:                       # shares the environment set up before it
            packages = next(p for e, p, _ in MODELS.values() if e == env and p)
        log(f"▶ {label}")
        out = os.path.join(out_dir, f"out-{name}.json")
        try:
            py = sys.executable if env == "system" else _venv_python(env, packages, log)
            proc = subprocess.Popen([py, os.path.abspath(__file__), "run", name, manifest_path, out],
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            tail = []
            for line in proc.stdout:
                tail = (tail + [line.rstrip()])[-40:]
                if line.startswith(("  ✓", "  ✗", "  ⚠")):
                    log(line.rstrip())
            proc.wait()
            if os.path.exists(out):
                with open(out, encoding="utf-8") as f:
                    results[name] = json.load(f)
            else:
                results[name] = {"_error": "\n".join(tail)}
                log("  ✗ failed:\n     " + "\n     ".join(tail[-15:]))
        except Exception as e:
            results[name] = {"_error": f"could not start: {e}"}
            log(f"  ✗ could not start: {e}")
    return results


def _transcriber(name: str):
    """(load → transcribe(wav_path) → text) for one model. Runs inside the child process."""
    import torch
    if name.startswith("whisper"):
        from faster_whisper import BatchedInferencePipeline, WhisperModel

        from colab_batch import ensure_cuda_libs     # CTranslate2 needs CUDA 12 libraries
        ensure_cuda_libs()
        model = WhisperModel("large-v3", device="cuda", compute_type="float16")
        m = re.search(r"-vad(\d\d)$", name)       # speech detector threshold (production: 0.35)
        vad = {"threshold": int(m.group(1)) / 100 if m else 0.35}
        prep = louder if "-loud" in name else (lambda a: a)
        if name.startswith("whisper-batched"):
            pipe = BatchedInferencePipeline(model)

            def run(audio):
                return pipe.transcribe(audio, language="de", batch_size=16, beam_size=5,
                                       vad_parameters=vad)[0]
        else:
            def run(audio):
                return model.transcribe(audio, language="de", beam_size=5, vad_filter=True,
                                        vad_parameters=vad, condition_on_previous_text=True)[0]

        def whisper(wav):                         # text + where (and how much) was heard
            segments = list(run(prep(_read_wav(wav))))
            return (" ".join(s.text.strip() for s in segments),
                    [[round(s.start), round(s.end), len(s.text.split())] for s in segments])
        return whisper

    if name.startswith("primeline"):
        from transformers import AutoModelForSpeechSeq2Seq, AutoProcessor, pipeline
        repo = {"primeline-german": "primeline/whisper-large-v3-german",
                "primeline-turbo-de": "primeline/whisper-large-v3-turbo-german"}[name]
        model = AutoModelForSpeechSeq2Seq.from_pretrained(repo, torch_dtype=torch.float16,
                                                          attn_implementation="sdpa",
                                                          low_cpu_mem_usage=True, use_safetensors=True)
        processor = AutoProcessor.from_pretrained(repo)
        pipe = pipeline("automatic-speech-recognition", model=model.to("cuda"),
                        tokenizer=processor.tokenizer, feature_extractor=processor.feature_extractor,
                        max_new_tokens=128, chunk_length_s=30, batch_size=4, return_timestamps=True,
                        torch_dtype=torch.float16, device="cuda")
        return lambda wav: pipe(wav, generate_kwargs={"language": "german", "task": "transcribe"})["text"]

    if name.startswith("voxtral"):
        from transformers import AutoProcessor, VoxtralForConditionalGeneration
        repo = "mistralai/Voxtral-Mini-3B-2507" if name == "voxtral-mini" else "mistralai/Voxtral-Small-24B-2507"
        kwargs = {"torch_dtype": torch.bfloat16, "device_map": "cuda"}
        if name == "voxtral-small-4bit":
            from transformers import BitsAndBytesConfig
            kwargs["quantization_config"] = BitsAndBytesConfig(
                load_in_4bit=True, bnb_4bit_quant_type="nf4", bnb_4bit_compute_dtype=torch.bfloat16)
        processor = AutoProcessor.from_pretrained(repo)
        model = VoxtralForConditionalGeneration.from_pretrained(repo, **kwargs)

        def voxtral(wav):
            inputs = processor.apply_transcription_request(language="de", audio=wav, model_id=repo)
            inputs = inputs.to("cuda", dtype=torch.bfloat16)
            out = model.generate(**inputs, max_new_tokens=3000)
            return processor.batch_decode(out[:, inputs.input_ids.shape[1]:], skip_special_tokens=True)[0]
        return voxtral

    if name == "qwen3-asr":
        from qwen_asr import Qwen3ASRModel
        model = Qwen3ASRModel.from_pretrained("Qwen/Qwen3-ASR-1.7B", dtype=torch.bfloat16,
                                              device_map="cuda:0", max_new_tokens=3000)
        return lambda wav: model.transcribe(audio=wav, language="German")[0].text

    raise ValueError(name)


def _child(name: str, manifest_path: str, out_path: str) -> None:
    import torch
    with open(manifest_path, encoding="utf-8") as f:
        manifest = json.load(f)
    if name == "voxtral-small-4bit":
        gb = torch.cuda.get_device_properties(0).total_memory / 1e9
        if gb < 22:
            print(f"  ⚠ skipped: needs a GPU with ≥ 22 GB (this one has {gb:.0f} GB) — use L4 or A100")
            return
    t0 = time.time()
    transcribe = _transcriber(name)
    print(f"  ✓ loaded in {time.time() - t0:.0f} s", flush=True)
    results = {}
    for clip in manifest:
        if clip.get("models") and name not in clip["models"]:
            continue
        t0 = time.time()
        try:
            out = transcribe(clip["wav"])
            text, spans = out if isinstance(out, tuple) else (out, None)
            results[clip["id"]] = {"text": text.strip(), "seconds": round(time.time() - t0, 1)}
            if spans is not None:
                results[clip["id"]]["spans"] = spans
            print(f"  ✓ {clip['id']}: {time.time() - t0:.0f} s", flush=True)
        except Exception as e:
            results[clip["id"]] = {"error": f"{type(e).__name__}: {e}"}
            print(f"  ✗ {clip['id']}: {type(e).__name__}: {e}", flush=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False)


# ─── Scoring and report ──────────────────────────────────────

def score(text: str, slides: str, minutes: float, zipf) -> dict:
    """Countable quality signals for one transcript (see the module docstring)."""
    words = WORD.findall(text)
    on_slides = {w.lower() for w in WORD.findall(slides)}
    odd = sorted({w for w in words if len(w) >= 6 and w.lower() not in on_slides
                  and zipf(w.lower()) < 1.5})
    terms = {w.lower() for w in WORD.findall(slides) if len(w) >= 8}
    said = {w.lower() for w in words}
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]
    return {"words_per_min": round(len(words) / minutes) if minutes else 0,
            "odd_per_1000": round(1000 * len(odd) / max(len(words), 1), 1),
            "odd_words": odd,
            "slide_terms": len(terms & said),
            "fillers": sum(1 for s in sentences if FILLER.match(s))}


def coverage(spans: list, seconds: float) -> str:
    """
    Which minutes got any text, the stretches over 2 minutes without any, and (long clips)
    words per 5 minutes. A segment counts for the minute it starts in: in batched mode one
    segment can bridge minutes the speech detector skipped, so its end proves nothing
    (round 2 reported 61 of 61 minutes for a recording that still had a 12-minute gap).
    """
    heard = set()
    per5 = [0] * (int(seconds // 300) + 1)
    for span in spans:
        heard.add(int(span[0] // 60))
        if len(span) > 2:
            per5[min(int(span[0] // 300), len(per5) - 1)] += span[2]
    total = max(1, int(-(-seconds // 60)))
    gaps, start = [], None
    for m in range(total + 1):
        if m < total and m not in heard:
            start = m if start is None else start
        elif start is not None:
            if m - start > 2:
                gaps.append(f"{start}–{m} min")
            start = None
    out = f"text in {len(heard & set(range(total)))} of {total} minutes" + \
        (f"; nothing in {', '.join(gaps)}" if gaps else "")
    if seconds > 600 and any(len(span) > 2 for span in spans):
        out += "\n           words per 5 min: " + " ".join(str(n) for n in per5)
    return out


def report(manifest: list[dict], results: dict, out_dir: str, seed: int | None = None,
           zipf=None) -> tuple[str, str]:
    """Blind texts (Version A, B, …) + key file with model names and scores. → paths."""
    if zipf is None:
        try:
            from wordfreq import zipf_frequency
        except ImportError:                       # the notebook installs it; this is a fallback
            if not sys.platform.startswith("linux"):
                raise
            subprocess.run([sys.executable, "-m", "pip", "install", "-q", "wordfreq"], check=True)
            from wordfreq import zipf_frequency
        zipf = lambda w: zipf_frequency(w, "de")    # noqa: E731
    names = sorted(results)
    random.Random(seed).shuffle(names)
    letter = dict(zip(names, string.ascii_uppercase))
    stamp = f"{datetime.now():%Y-%m-%d-%H%M}"
    blind = [f"Model comparison {stamp} — which version is closest to what was said?", ""]
    key = [f"Key to Model-Comparison-{stamp}.txt", ""]
    key += [f"Version {letter[n]} = {MODELS[n][2]}" for n in names] + [""]
    for n in names:
        if "_error" in results[n]:
            key += [f"✗ {MODELS[n][2]} failed as a whole — last output:",
                    *("   " + line for line in results[n]["_error"].splitlines()), ""]
    for clip in manifest:
        minutes = clip["seconds"] / 60
        blind += ["=" * 78, f"{clip['id']}: {clip.get('why', '')}", "=" * 78, ""]
        key += [f"── {clip['id']} ({clip.get('why', '')})",
                f"   {'version':8}{'model':22}{'words/min':>10}{'odd/1000':>10}{'slide terms':>12}"
                f"{'fillers':>9}{'time s':>8}"]
        for n in names:
            if clip.get("models") and n not in clip["models"]:
                continue
            r = results[n].get(clip["id"], {"error": "model failed (see top of the key)"
                                            if "_error" in results[n] else "not run"})
            blind += [f"── Version {letter[n]} ──", r.get("text") or f"(failed: {r.get('error')[:300]})", ""]
            if "text" in r:
                s = score(r["text"], clip.get("slides", ""), minutes, zipf)
                key.append(f"   {letter[n]:8}{n:22}{s['words_per_min']:>10}{s['odd_per_1000']:>10}"
                           f"{s['slide_terms']:>12}{s['fillers']:>9}{r['seconds']:>8}")
                key.append(f"   {'':8}odd words: {', '.join(s['odd_words'][:25])}")
                if r.get("spans") is not None:
                    key.append(f"   {'':8}{coverage(r['spans'], clip['seconds'])}")
            else:
                key.append(f"   {letter[n]:8}{n:22}  failed: {r.get('error')[:300]}")
        loud = clip.get("loudness", [])
        if loud:
            key.append(f"   whole recording, loudness per minute (dBFS): {loud}")
        key.append("")
    paths = (os.path.join(out_dir, f"Model-Comparison-{stamp}.txt"),
             os.path.join(out_dir, f"Model-Comparison-{stamp}-key.txt"))
    for path, lines in zip(paths, (blind, key)):
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    return paths


if __name__ == "__main__" and len(sys.argv) == 5 and sys.argv[1] == "run":
    _child(*sys.argv[2:])
