"""Per-dog identification: ingest human labels, train a classifier, predict.

Runs after `analyze`, before `export`. Uses the per-event embeddings stored by
`analyze` as features and the human labels exported from the website
(`labels.json`) as training targets. Human labels always win; the trained model
only fills in a *suggested* dog for the remaining (unlabeled) events.

The classifier is a small scikit-learn model — trained in seconds on CPU — over
frozen embeddings. Nothing here fine-tunes or touches the detector.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import datetime, timezone

import numpy as np

from .store import Store

log = logging.getLogger(__name__)

# Labels that are never predicted / never used as training classes for "which dog".
NON_DOG_LABELS = {"unsure", "multiple", "not_a_dog"}


def _ingest_labels(cfg, store: Store) -> int:
    """Load labels.json (if present) into the event_labels table. Returns count."""
    path = cfg.resolve_path(cfg.identification.labels_path)
    if not path.exists():
        log.info("  no labels file at %s (skipping label ingest)", path)
        return 0
    data = json.loads(path.read_text(encoding="utf-8"))
    labels = data.get("labels", {})
    now = datetime.now(timezone.utc).isoformat()
    for key, label in labels.items():
        store.upsert_label(key, str(label), "human", now)
    store.commit()
    log.info("  ingested %d human labels from %s", len(labels), path)
    return len(labels)


def _build_estimator(cfg, n_samples: int):
    """Construct a fresh (unfitted) sklearn pipeline per config."""
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    if cfg.identification.classifier == "knn":
        from sklearn.neighbors import KNeighborsClassifier
        clf = KNeighborsClassifier(n_neighbors=max(1, min(5, n_samples // 3)))
    else:
        from sklearn.linear_model import LogisticRegression
        clf = LogisticRegression(max_iter=1000, class_weight="balanced")
    return make_pipeline(StandardScaler(), clf)


def _cv_metrics(cfg, X, y, classes: list[str]) -> dict:
    """Cross-validated accuracy, per-dog precision/recall/f1, and confusion matrix.

    Uses held-out folds (honest estimate), not resubstitution. Needs >=2 examples
    per dog to stratify; otherwise reports that CV was not possible.
    """
    counts = Counter(y)
    min_count = min(counts.values())
    if min_count < 2:
        return {"cv_available": False,
                "reason": "need >=2 examples per dog for cross-validation"}
    from sklearn.model_selection import StratifiedKFold, cross_val_predict
    from sklearn.metrics import (accuracy_score, confusion_matrix,
                                 precision_recall_fscore_support)
    folds = int(min(5, min_count))
    skf = StratifiedKFold(n_splits=folds, shuffle=True, random_state=0)
    y_pred = cross_val_predict(_build_estimator(cfg, len(y)), np.asarray(X), y, cv=skf)
    labels = sorted(classes)
    p, r, f, s = precision_recall_fscore_support(y, y_pred, labels=labels, zero_division=0)
    cm = confusion_matrix(y, y_pred, labels=labels)
    per_dog = {lbl: {"precision": round(float(p[i]), 3), "recall": round(float(r[i]), 3),
                     "f1": round(float(f[i]), 3), "support": int(s[i])}
               for i, lbl in enumerate(labels)}
    return {"cv_available": True, "cv_folds": folds,
            "accuracy": round(float(accuracy_score(y, y_pred)), 3),
            "labels": labels, "confusion_matrix": cm.tolist(), "per_dog": per_dog}


def _train(cfg, store: Store, labels: dict[str, str]):
    """Train + evaluate a classifier on labeled embeddings.

    Returns (model, metrics). model is None (with a metrics dict explaining why)
    when there aren't enough labels yet.
    """
    rows = store.events_with_embeddings()
    X, y = [], []
    for r in rows:
        lbl = labels.get(r["event_key"])
        if lbl and lbl not in NON_DOG_LABELS:
            X.append(np.asarray(json.loads(r["embedding"]), dtype=np.float32))
            y.append(lbl)

    counts = Counter(y)
    min_n = cfg.identification.min_labels_per_dog
    trainable = sorted(d for d, n in counts.items() if n >= min_n)
    below = {d: n for d, n in counts.items() if n < min_n}
    if below:
        log.info("  dogs below min_labels_per_dog(%d), not yet learned: %s", min_n, below)

    if len(trainable) < 2:
        log.info("  not enough labels to train: need >=%d each for >=2 dogs, have %s",
                 min_n, dict(counts))
        return None, {"trained": False,
                      "reason": f"need >={min_n} labels for >=2 dogs",
                      "label_counts": dict(counts)}

    Xt = np.vstack([x for x, lbl in zip(X, y) if lbl in trainable])
    yt = [lbl for lbl in y if lbl in trainable]

    model = _build_estimator(cfg, len(yt))
    model.fit(Xt, yt)

    import joblib
    model_path = cfg.resolve_path(cfg.identification.model_path)
    model_path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, model_path)

    metrics = {"trained": True,
               "trained_at": datetime.now(timezone.utc).isoformat(),
               "classifier": cfg.identification.classifier,
               "embedding": cfg.identification.embedding,
               "dogs": trainable,
               "n_labeled": len(yt),
               "label_counts": dict(counts),
               **_cv_metrics(cfg, Xt, yt, trainable)}
    log.info("  trained %s on %d labels across %d dogs %s -> %s",
             cfg.identification.classifier, len(yt), len(trainable), trainable, model_path)
    if metrics.get("cv_available"):
        log.info("  cross-validated accuracy: %.1f%% (%d-fold)",
                 metrics["accuracy"] * 100, metrics["cv_folds"])
        for d, m in metrics["per_dog"].items():
            log.info("    %-14s precision %.2f  recall %.2f  (n=%d)",
                     d, m["precision"], m["recall"], m["support"])
        log.info("  confusion matrix (rows=true, cols=pred) %s:", metrics["labels"])
        for lbl, row in zip(metrics["labels"], metrics["confusion_matrix"]):
            log.info("    %-14s %s", lbl, row)
    return model, metrics


def _predict_all(store: Store, labels: dict[str, str], model):
    """Resolve dog_label for every event: human label wins, else model prediction."""
    rows = store.events_with_embeddings()
    n_human = n_pred = 0
    for r in rows:
        human = labels.get(r["event_key"])
        if human:
            store.set_event_prediction(r["id"], human, None, "human")
            n_human += 1
        elif model is not None:
            vec = np.asarray(json.loads(r["embedding"]), dtype=np.float32).reshape(1, -1)
            proba = model.predict_proba(vec)[0]
            k = int(np.argmax(proba))
            store.set_event_prediction(r["id"], str(model.classes_[k]),
                                       round(float(proba[k]), 4), "predicted")
            n_pred += 1
        else:
            store.set_event_prediction(r["id"], None, None, None)
    store.commit()
    log.info("  resolved dogs: %d human, %d predicted", n_human, n_pred)


def identify(cfg, store: Store) -> dict:
    """Ingest labels, (re)train the dog classifier, and predict for all events."""
    if not cfg.identification.enabled:
        log.info("  identification disabled")
        return {"trained": False}
    _ingest_labels(cfg, store)
    labels = store.all_labels()
    model, metrics = _train(cfg, store, labels)
    store.set_meta("identification_metrics", json.dumps(metrics, ensure_ascii=False))
    store.commit()
    _predict_all(store, labels, model)
    return {"trained": model is not None, "labels": len(labels)}
