"""Model comparison harness: clips, scoring, blind report (no models run here)."""

import os
import re

import model_compare as mc
from test_colab_batch import _goodnotes


def test_score_counts_what_matters():
    zipf = {"skalengeduld": 0.0, "prothesen": 2.1}.get
    s = mc.score("Wir messen das Skalenniveau. Vielen Dank. Die Skalengeduld und Prothesen.",
                 slides="Skalenniveau Hypothesen", minutes=0.1, zipf=lambda w: zipf(w, 4.0))
    assert s["odd_words"] == ["Skalengeduld"]          # 'Prothesen' is a real (wrong) word
    assert s["slide_terms"] == 1 and s["fillers"] == 1 and s["words_per_min"] == 100


def test_prepare_and_blind_report(tmp_path):
    backup = tmp_path / "MyDrive" / "GoodNotes" / "M13"
    os.makedirs(backup)
    _goodnotes(str(backup / "6_Inferenzstatistik_v42894 2.goodnotes"),
               [("8662089F-E428", 120, (2026, 10, 16, 11, 2, 0), 220)])
    clips = [{"id": "hard", "notebook": "6_Inferenzstatistik", "recording": "8662089F",
              "start_min": 0.5, "minutes": 1, "why": "test"},
             {"id": "missing", "notebook": "nope", "recording": "x", "start_min": 0, "minutes": 1}]
    manifest = mc.prepare(str(tmp_path / "MyDrive"), clips, str(tmp_path / "work"), log=lambda *_: None)
    assert [c["id"] for c in manifest] == ["hard"] and manifest[0]["seconds"] == 60.0
    assert len(manifest[0]["loudness"]) == 2                # 2 minutes of tone, per minute

    results = {"whisper-batched": {"hard": {"text": "Das ist die Inferenzstatistik.", "seconds": 3.0}},
               "qwen3-asr": {"hard": {"error": "RuntimeError: boom"}}}
    texts, key = mc.report(manifest, results, str(tmp_path), seed=1, zipf=lambda w: 4.0)
    blind, key = open(texts, encoding="utf-8").read(), open(key, encoding="utf-8").read()
    assert "whisper" not in blind.lower() and "qwen" not in blind.lower()   # names only in the key
    assert "Das ist die Inferenzstatistik." in blind and "(failed: RuntimeError: boom)" in blind
    letter = re.search(r"Version (\w) = Whisper large-v3, batched", key).group(1)
    assert re.search(rf"\n   {letter}\s+whisper-batched\s+4\b", key)  # 4 words in 1 minute
