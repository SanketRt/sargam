"""
The anti-fabrication pass.

Rendering is the one place a model is allowed to produce prose, so it is the
one place that can invent. Every rendered paragraph is checked sentence by
sentence against the fragments it was derived from, and the verdicts are
stored, not just logged: the review UI shows you which sentences are the
model's inference rather than your words.

`unsupported` sentences are removed from the manuscript. That is a deliberate
asymmetry -- a memoir that is missing a sentence is recoverable, a memoir that
contains a fact you never said is not.
"""

from __future__ import annotations

import json

import extract

SUPPORTED = "supported"
INFERRED = "inferred"
UNSUPPORTED = "unsupported"


def check(store, paragraph_id: str, body: str, sources: dict[str, str],
          api_key: str | None = None) -> list[dict]:
    """Ground one paragraph and persist the verdicts."""
    try:
        out = extract.ground(body, sources, api_key=api_key)
    except Exception as exc:                     # network, auth, refusal
        print(f"  grounding skipped for {paragraph_id}: {exc}")
        return []
    verdicts = out.get("sentences", [])
    store.set_groundings(paragraph_id, verdicts)
    return verdicts


def from_rows(rows) -> list[dict]:
    """Store rows -> the shape the rest of this module speaks.

    The table names the column `sentence_ix` and keeps `evidence` as JSON;
    everything in memory uses `ix` and a list. Without this adapter the
    mismatch hides inside a comprehension that only evaluates `v["ix"]` for
    an unsupported sentence -- so it would work perfectly until the first
    time the grounder actually caught a fabrication.
    """
    out = []
    for r in rows:
        d = dict(r)
        ev = d.get("evidence")
        if isinstance(ev, str):
            try:
                ev = json.loads(ev)
            except (ValueError, TypeError):
                ev = []
        out.append({"ix": d.get("ix", d.get("sentence_ix")),
                    "verdict": d.get("verdict", "unchecked"),
                    "evidence": ev or []})
    return out


def strip_unsupported(body: str, verdicts: list[dict]) -> tuple[str, int]:
    """Drop sentences the grounder could not support. Returns (body, n_dropped)."""
    if not verdicts:
        return body, 0
    sents = extract.sentences_of(body)
    bad = {v["ix"] for v in verdicts if v.get("verdict") == UNSUPPORTED}
    if not bad:
        return body, 0
    kept = [s for i, s in enumerate(sents) if i not in bad]
    return " ".join(kept), len(bad)


def annotate(body: str, verdicts: list[dict]) -> list[dict]:
    """Sentence-level view for the review UI."""
    sents = extract.sentences_of(body)
    by_ix = {v["ix"]: v for v in verdicts}
    out = []
    for i, s in enumerate(sents):
        v = by_ix.get(i, {})
        out.append({
            "ix": i,
            "text": s,
            "verdict": v.get("verdict", "unchecked"),
            "evidence": v.get("evidence", []),
        })
    return out


def report(store) -> dict:
    """Counts across the whole manuscript, so a compile can say whether the
    grounding picture got better or worse."""
    counts = {SUPPORTED: 0, INFERRED: 0, UNSUPPORTED: 0}
    worst: list[tuple[str, int]] = []
    for row in store.paragraphs():
        vs = store.groundings(row["id"])
        n_bad = 0
        for v in vs:
            if v["verdict"] in counts:
                counts[v["verdict"]] += 1
            if v["verdict"] == UNSUPPORTED:
                n_bad += 1
        if n_bad:
            worst.append((row["id"], n_bad))
    worst.sort(key=lambda t: -t[1])
    return {"counts": counts, "worst": worst[:10]}
