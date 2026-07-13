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


def _train(cfg, store: Store, labels: dict[str, str]):
    """Train a classifier on labeled embeddings. Returns (model, classes) or None."""
    rows = store.events_with_embeddings()
    X, y = [], []
    for r in rows:
        lbl = labels.get(r["event_key"])
        if lbl and lbl not in NON_DOG_LABELS:
            X.append(np.asarray(json.loads(r["embedding"]), dtype=np.float32))
            y.append(lbl)
    if not X:
        log.info("  no usable dog labels yet — skipping training")
        return None

    counts = Counter(y)
    min_n = cfg.identification.min_labels_per_dog
    trainable = {d for d, n in counts.items() if n >= min_n}
    if len(trainable) < 2:
        log.info("  not enough labels to train: need >=%d each for >=2 dogs, have %s",
                 min_n, dict(counts))
        return None

    X = np.vstack([x for x, lbl in zip(X, y) if lbl in trainable])
    y = [lbl for lbl in y if lbl in trainable]

    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    if cfg.identification.classifier == "knn":
        from sklearn.neighbors import KNeighborsClassifier
        clf = KNeighborsClassifier(n_neighbors=min(5, len(y)))
    else:
        from sklearn.linear_model import LogisticRegression
        clf = LogisticRegression(max_iter=1000, class_weight="balanced")
    model = make_pipeline(StandardScaler(), clf)
    model.fit(X, y)

    import joblib
    model_path = cfg.resolve_path(cfg.identification.model_path)
    model_path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, model_path)
    log.info("  trained %s on %d labels across %d dogs %s -> %s",
             cfg.identification.classifier, len(y), len(trainable),
             sorted(trainable), model_path)
    return model


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
    model = _train(cfg, store, labels)
    _predict_all(store, labels, model)
    return {"trained": model is not None, "labels": len(labels)}
