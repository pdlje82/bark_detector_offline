"""Unit tests for the pure logic (no audio model / ffmpeg required).

Run with:  python -m pytest   (or: python tests/test_logic.py)
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))

from barkdetect.audio import normalize_window
from barkdetect.coverage import compute_coverage
from barkdetect.detect import extract_events


def _det_cfg(threshold=0.5, merge_gap=0.4, min_event=0.15):
    return SimpleNamespace(detection=SimpleNamespace(
        threshold=threshold, merge_gap_seconds=merge_gap,
        min_event_seconds=min_event))


# --- normalization ---------------------------------------------------------

def test_normalize_boosts_quiet_audio():
    cfg = SimpleNamespace(enabled=True, target_peak=0.9, max_gain=20.0, noise_floor=0.005)
    quiet = np.array([0.05, -0.05, 0.025], dtype=np.float32)
    out = normalize_window(quiet, cfg)
    assert abs(np.max(np.abs(out)) - 0.9) < 1e-5


def test_normalize_silence_guard():
    cfg = SimpleNamespace(enabled=True, target_peak=0.9, max_gain=20.0, noise_floor=0.005)
    silence = np.array([0.001, -0.002, 0.0], dtype=np.float32)
    out = normalize_window(silence, cfg)
    np.testing.assert_array_equal(out, silence)  # untouched


def test_normalize_gain_cap():
    cfg = SimpleNamespace(enabled=True, target_peak=0.9, max_gain=2.0, noise_floor=0.005)
    arr = np.array([0.1, -0.1], dtype=np.float32)  # would need 9x, capped at 2x
    out = normalize_window(arr, cfg)
    assert abs(np.max(np.abs(out)) - 0.2) < 1e-5


# --- event extraction ------------------------------------------------------

def test_extract_single_event():
    # 10 fps timeline, 2s of "hot" in the middle
    times = np.arange(0, 5, 0.1)
    scores = np.zeros_like(times)
    scores[20:40] = 0.8              # 2.0s .. 4.0s
    best = np.zeros_like(times, dtype=int)
    energy = np.zeros_like(times)
    energy[25] = 0.42                # loudest instant within the event
    events = extract_events(times, scores, best, energy, ["Dog", "Bark"], _det_cfg())
    assert len(events) == 1
    assert events[0]["top_class"] == "Dog"
    assert 1.9 < events[0]["offset_start_sec"] < 2.1
    assert events[0]["duration_sec"] > 1.5
    assert events[0]["intensity_raw"] == 0.42   # peak raw energy over the span


def test_merge_close_events_and_drop_short():
    times = np.arange(0, 5, 0.1)
    scores = np.zeros_like(times)
    scores[10:13] = 0.9              # 0.3s span
    scores[15:18] = 0.9             # gap of 0.2s -> merged
    scores[45:46] = 0.9             # 0.1s lone spike -> dropped (min 0.15s)
    best = np.zeros_like(times, dtype=int)
    energy = np.zeros_like(times)
    events = extract_events(times, scores, best, energy, ["Dog"], _det_cfg())
    assert len(events) == 1         # two merged, short one dropped


def test_librosa_features_fixed_length():
    """Embedding features are a fixed-length, finite 1-D vector regardless of input length."""
    from barkdetect.embeddings import _librosa_features
    sr = 32000
    rng = np.random.default_rng(0)
    short = rng.standard_normal(sr // 2).astype(np.float32)   # 0.5s
    long = rng.standard_normal(sr * 3).astype(np.float32)     # 3s
    v1 = _librosa_features(short, sr)
    v2 = _librosa_features(long, sr)
    assert v1.ndim == 1 and v1.shape == v2.shape and v1.size > 0
    assert np.isfinite(v1).all() and np.isfinite(v2).all()


def _seed_events_with_embeddings(store, keys_labels, dim=8):
    """Insert one recording and events with 2-cluster embeddings; return {key: vec}."""
    rng = np.random.default_rng(1)
    rid = store.add_recording(dict(
        sha256="s"*64, original_filename="R.mp3", archived_path="x", file_size=1,
        duration_sec=100.0, sample_rate=32000, start_utc="2026-07-06T00:00:00+00:00",
        start_local="2026-07-06T02:00:00+02:00", timezone="Europe/Berlin",
        timestamp_source="t", mtime_utc="2026-07-06T00:00:00+00:00",
        ingested_at="2026-07-06T00:00:00+00:00"))
    import json as _json
    centers = {"rex": np.array([3.0] + [0]*(dim-1)), "bella": np.array([0, 3.0] + [0]*(dim-2))}
    for i, (key, label) in enumerate(keys_labels):
        base = centers["rex"] if (label == "rex" or (label is None and i % 2 == 0)) else centers["bella"]
        vec = (base + rng.standard_normal(dim) * 0.2).astype(np.float32)
        store.add_event(dict(recording_id=rid, offset_start_sec=float(i), offset_end_sec=float(i)+0.5,
            abs_start_utc="2026-07-06T00:00:%02d+00:00" % i, abs_end_utc="2026-07-06T00:00:%02d+00:00" % i,
            duration_sec=0.5, peak_conf=0.9, mean_conf=0.8, top_class="Bark",
            event_key=key, embedding=_json.dumps(vec.tolist())))
    store.commit()


def test_identify_train_and_predict(tmp_path):
    """Human labels win; the classifier predicts a dog for unlabeled events."""
    from barkdetect.store import Store
    from barkdetect.identify import _train, _predict_all
    store = Store(tmp_path / "t.db")
    labeled = [(f"k_rex_{i}", "rex") for i in range(4)] + [(f"k_bella_{i}", "bella") for i in range(4)]
    unlabeled = [("k_new_0", None), ("k_new_1", None)]
    _seed_events_with_embeddings(store, labeled + unlabeled)
    now = "2026-07-06T00:00:00+00:00"
    for key, lbl in labeled:
        store.upsert_label(key, lbl, "human", now)
    store.commit()

    cfg = SimpleNamespace(identification=SimpleNamespace(
        min_labels_per_dog=3, classifier="logreg", embedding="librosa",
        model_path=str(tmp_path / "clf.joblib")),
        resolve_path=lambda p: tmp_path / str(p))
    labels = store.all_labels()
    model, metrics = _train(cfg, store, labels)
    assert model is not None
    _predict_all(store, labels, model)

    evs = {e["event_key"]: e for e in store.all_events()}
    assert evs["k_rex_0"]["dog_label_source"] == "human"
    assert evs["k_new_0"]["dog_label_source"] == "predicted"
    assert evs["k_new_0"]["dog_label"] in {"rex", "bella"}
    assert 0.0 <= evs["k_new_0"]["dog_confidence"] <= 1.0

    # cross-validation metrics + confusion matrix
    assert metrics["trained"] and metrics["cv_available"]
    assert set(metrics["per_dog"]) == {"rex", "bella"}
    assert metrics["labels"] == ["bella", "rex"]
    assert len(metrics["confusion_matrix"]) == 2 and len(metrics["confusion_matrix"][0]) == 2
    assert 0.0 <= metrics["accuracy"] <= 1.0


def test_export_includes_identity(tmp_path):
    """build_export surfaces dogs roster, per-event key and dog fields, and per-dog daily counts."""
    from barkdetect.config import Config
    from barkdetect.store import Store
    from barkdetect.export import build_export
    cfg = Config.load(str(_REPO / "config.yml"))
    store = Store(tmp_path / "t.db")
    import json as _json
    _seed_events_with_embeddings(store, [("k0", "rex"), ("k1", "bella")])
    # event 1 = a human multi-dog label (both rex and bella heard)
    store.set_event_prediction(1, "rex", _json.dumps(["rex", "bella"]), None, "human")
    store.set_event_prediction(2, "bella", None, 0.77, "predicted")
    data = build_export(cfg, store)
    assert data["schema_version"] == 2
    assert isinstance(data["dogs"], list)
    e0 = data["events"][0]
    assert set(["key", "dog_label", "dog_labels", "dog_confidence", "dog_label_source"]).issubset(e0)
    assert e0["dog_labels"] == ["rex", "bella"]          # multi-dog preserved
    assert e0["dog_label"] == "rex"                       # primary for back-compat
    by_dog = data["daily_summary"][0]["by_dog"]
    assert by_dog.get("rex", 0) >= 1 and by_dog.get("bella", 0) >= 1   # both credited


def test_label_api_roundtrip(tmp_path):
    """The Flask label API writes/reads/deletes labels directly in the DB."""
    from barkdetect.serve import _build_app
    from barkdetect.store import Store
    db = tmp_path / "barks.db"
    Store(str(db)).close()                                    # create the DB/schema
    (tmp_path / "frontend").mkdir()
    (tmp_path / "export").mkdir()
    cfg = SimpleNamespace(
        project_root=tmp_path,
        export=SimpleNamespace(filename="results.json"),
        path=lambda k: {"export_dir": tmp_path / "export",
                        "snippets_dir": tmp_path / "snippets",
                        "db_path": db}[k])
    client = _build_app(cfg).test_client()

    assert client.get("/api/labels").get_json() == {}
    assert client.put("/api/labels/k1", json={"label": "Podenco"}).status_code == 200
    client.put("/api/labels/k2", json={"label": ["Podenco", "Clooney"]})     # multi-dog
    labels = client.get("/api/labels").get_json()
    assert labels["k1"] == "Podenco"
    assert labels["k2"] == ["Podenco", "Clooney"]              # array round-trips
    assert client.put("/api/labels/k3", json={}).status_code == 400   # missing label
    client.delete("/api/labels/k1")
    assert "k1" not in client.get("/api/labels").get_json()
    # region-audio endpoint: validation + unknown-recording (no ffmpeg needed)
    assert client.get("/api/audio/abc?start=0&dur=0").status_code == 400      # dur must be > 0
    assert client.get("/api/audio/deadbeef?start=0&dur=1").status_code == 404  # no such recording
    # written straight to the DB
    assert Store(str(db)).all_labels() == {"k2": '["Podenco", "Clooney"]'}


def test_enhance_filter_string():
    from barkdetect.enhance import filter_string
    chain = [{"loudnorm": {"I": -16, "TP": -1.5, "LRA": 11}},
             {"bandpass": {"low": 150, "high": 12000}},
             {"denoise": {"filter": "afftdn"}}]
    fs = filter_string(chain)
    assert "loudnorm=I=-16:TP=-1.5:LRA=11" in fs
    assert "highpass=f=150" in fs and "lowpass=f=12000" in fs
    assert "afftdn" in fs
    assert filter_string([]) == ""


def test_enhanced_path_resolution(tmp_path):
    from barkdetect.enhance import enhanced_path
    cfg = SimpleNamespace(
        enhancement=SimpleNamespace(dir="enh", format="mp3"),
        resolve_path=lambda p: tmp_path / str(p))
    p = enhanced_path(cfg, "/whatever/data/archive/260706_x_ZOOM0007.MP3")
    assert p == tmp_path / "enh" / "260706_x_ZOOM0007.mp3"


def test_delete_and_count_labels(tmp_path):
    from barkdetect.store import Store
    store = Store(tmp_path / "t.db")
    store.upsert_label("k1", "rex", "human", "t")
    store.upsert_label("k2", "bella", "human", "t")
    store.commit()
    assert store.count_labels() == 2
    assert store.delete_label("k1") == 1
    assert store.delete_label("k1") == 0          # already gone
    assert store.count_labels() == 1
    assert store.all_labels() == {"k2": "bella"}
    assert store.clear_labels() == 1
    assert store.count_labels() == 0


def test_segmentation_fingerprint_sensitivity():
    from barkdetect.guard import segmentation_fingerprint as fp

    def mk(threshold=0.15, merge=0.4, padding=2.0):
        return SimpleNamespace(
            detection=SimpleNamespace(threshold=threshold, min_event_seconds=0.15,
                                      merge_gap_seconds=merge, dog_classes=["Dog", "Bark"]),
            normalization=SimpleNamespace(enabled=True, target_peak=0.9, max_gain=20.0, noise_floor=0.005),
            audio=SimpleNamespace(sample_rate=32000, window_seconds=60, min_window_seconds=1.0),
            snippets=SimpleNamespace(padding_seconds=padding))

    base = fp(mk())
    assert fp(mk()) == base                                  # stable for equal config
    assert fp(mk(merge=0.2)) != base                          # segmentation change → differs
    assert fp(mk(threshold=0.2)) != base
    assert fp(mk(padding=1.0)) == base                        # snippet change → unaffected


def test_segmentation_guard(tmp_path, monkeypatch):
    """Guard clears labels only when segmentation changed, labels exist, and confirmed."""
    from barkdetect.store import Store
    from barkdetect.guard import enforce_guard
    store = Store(tmp_path / "t.db")
    store.upsert_label("k1", "rex", "human", "t"); store.commit()

    # matching fingerprint -> no-op, labels kept
    store.set_meta("segmentation_fingerprint", "FP_A"); store.commit()
    enforce_guard(SimpleNamespace(), store, "FP_A")
    assert store.count_labels() == 1

    # changed fingerprint, non-interactive with ASSUME_YES -> clears
    monkeypatch.setenv("BARKDETECT_ASSUME_YES", "1")
    enforce_guard(SimpleNamespace(), store, "FP_B")
    assert store.count_labels() == 0

    # changed fingerprint but no labels -> no error
    enforce_guard(SimpleNamespace(), store, "FP_C")


def test_publish_bundle(tmp_path):
    """publish assembles index.html + real results.json + snippets, excludes the rest."""
    from barkdetect.publish import publish
    root = tmp_path
    # repo-side frontend/
    fe = root / "frontend"; fe.mkdir()
    (fe / "index.html").write_text("<html>app</html>", encoding="utf-8")
    (fe / "results.json").write_text('{"sample": true}', encoding="utf-8")  # sample, must NOT win
    # data-side dirs
    (root / "export").mkdir()
    (root / "export" / "results.json").write_text('{"real": true}', encoding="utf-8")
    snips = root / "snippets" / "abc"; snips.mkdir(parents=True)
    (snips / "clip.mp3").write_bytes(b"AUDIO")
    (root / "archive").mkdir(); (root / "archive" / "ZOOM.MP3").write_bytes(b"HUGE")  # must NOT be published

    cfg = SimpleNamespace(
        project_root=root,
        export=SimpleNamespace(filename="results.json"),
        path=lambda k: {"site_dir": root / "site", "export_dir": root / "export",
                        "snippets_dir": root / "snippets"}[k])
    publish(cfg, store=None)

    site = root / "site"
    assert (site / "index.html").read_text(encoding="utf-8") == "<html>app</html>"
    assert (site / "results.json").read_text(encoding="utf-8") == '{"real": true}'  # real, not sample
    assert (site / "snippets" / "abc" / "clip.mp3").read_bytes() == b"AUDIO"
    assert not (site / "archive").exists()                    # originals never published


def test_snippet_name_template():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from barkdetect.snippets import snippet_relpath
    cfg = SimpleNamespace(snippets=SimpleNamespace(
        name_template="{date}_{time}_{ms}_{dbfs}", extension="mp3"))
    dt = datetime(2026, 7, 12, 17, 5, 2, tzinfo=ZoneInfo("Europe/Berlin"))
    rel = snippet_relpath(cfg, "9ab6bed4511d0000", dt, 33.274, 0.0342)
    # <hash>/ddmmyy_hhmmss_ms_dbfs.mp3 ; 20*log10(0.0342) ~ -29 dBFS
    assert rel == "9ab6bed4511d/120726_170502_000033274_-29dBFS.mp3"


def test_enforce_min_interval():
    from barkdetect.onset import _enforce_min_interval
    # 0.0 kept; 0.05 dropped (<0.12); 0.20 kept; 0.25 dropped; 0.40 kept
    assert _enforce_min_interval([0.0, 0.05, 0.20, 0.25, 0.40], 0.12) == [0.0, 0.20, 0.40]
    assert _enforce_min_interval([0.40, 0.0, 0.20], 0.12) == [0.0, 0.20, 0.40]  # sorts
    assert _enforce_min_interval([], 0.12) == []


def test_onset_flag_gates_slicing():
    """With use_onset_detection False, extract_events ignores audio_path (unchanged)."""
    times = np.arange(0, 5, 0.1)
    scores = np.zeros_like(times); scores[20:40] = 0.8
    best = np.zeros_like(times, dtype=int)
    energy = np.zeros_like(times); energy[25] = 0.42
    cfg = SimpleNamespace(
        detection=SimpleNamespace(threshold=0.5, merge_gap_seconds=0.4, min_event_seconds=0.15),
        onset=SimpleNamespace(use_onset_detection=False))
    events = extract_events(times, scores, best, energy, ["Dog"], cfg, audio_path="ignored.mp3")
    assert len(events) == 1                    # not sliced — same as onset-off
    assert events[0]["intensity_raw"] == 0.42


def test_frame_energy_rms_and_peak():
    from barkdetect.detect import _frame_energy
    raw = np.array([0.0, 0.0, 1.0, -1.0, 0.5, 0.5, 0.0, 0.0], dtype=np.float32)
    peak = _frame_energy(raw, 2, "peak")        # two frames of 4 samples
    assert list(peak) == [1.0, 0.5]
    rms = _frame_energy(raw, 2, "rms")
    assert abs(rms[0] - np.sqrt(0.5)) < 1e-6      # sqrt(mean(0,0,1,1)) = 0.707
    assert abs(rms[1] - np.sqrt(0.125)) < 1e-6    # sqrt(mean(.25,.25,0,0)) = 0.354


# --- coverage & gaps -------------------------------------------------------

def test_coverage_gap_and_contiguous():
    recs = [
        # two back-to-back files (H6 split) -> one covered span, no gap
        {"start_utc": "2026-07-05T18:00:00+00:00", "duration_sec": 3600},
        {"start_utc": "2026-07-05T19:00:00+00:00", "duration_sec": 3600},
        # later file after a gap
        {"start_utc": "2026-07-06T08:00:00+00:00", "duration_sec": 1800},
    ]
    coverage, gaps = compute_coverage(recs, merge_gap_seconds=5.0)
    assert len(coverage) == 2       # [18:00-20:00], [08:00-08:30]
    assert len(gaps) == 1
    assert abs(gaps[0]["duration_sec"] - 12 * 3600) < 1


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
