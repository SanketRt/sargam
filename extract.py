"""
The only three jobs the model is allowed to do: turn text into candidate
events, turn prose back into a grounding verdict, and write a paragraph
(render.py owns that last one). It never manages state -- it emits candidates,
the solver decides what is true.

Two backends:

  api      the real thing. Needs `pip install anthropic` and a credential
           (ANTHROPIC_API_KEY, or an `ant auth login` profile).
  offline  a rule-based extractor with no network. Deliberately narrow: it
           reads explicit dates and a few relative phrasings and pushes
           everything else onto the unresolved queue. It exists so the
           pipeline is runnable and testable without a key, not to compete
           with the model.

Set SARGAM_BACKEND=offline to force the rule-based path.
"""

from __future__ import annotations

import json
import os
import re

from timeline import PROV_ABSOLUTE, PROV_STATED, YEAR

MODEL = os.environ.get("SARGAM_MODEL", "claude-opus-5")


def backend(api_key: str | None = None) -> str:
    """Which path a call will take. `api_key` is the caller's own credential --
    in a hosted, multi-tenant setting the key arrives per request rather than
    from the process environment, and a user who supplied one is on the api
    path regardless of how the server itself is configured."""
    b = os.environ.get("SARGAM_BACKEND")
    if b == "offline":
        return b
    if api_key:
        return "api"
    if b:
        return b
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return "api"
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return "offline"
    # An `ant auth login` profile is resolved by the SDK itself; assume api and
    # let the first call raise a clear AuthenticationError if there is none.
    return "api"


EXTRACT_SYSTEM = """\
You convert one piece of autobiographical text into temporal facts.

Rules:
- `of`/`from`/`to`/`a`/`b` may be a tmp_id from this response or an existing
  event id supplied in the user turn. Never invent an id.
- Widen rather than guess. "a couple of years" is [1.5, 3.0], not [2.0, 2.0].
- A reference you cannot ground goes in `unresolved` verbatim. Do not fabricate
  a constraint to make the text look placed.
- Split compound recollections into separate events. One event, one happening.
- Fields that do not apply to a constraint kind are null. `absolute` uses
  of/lo/hi as ISO dates; `gap` uses from/to/lo_years/hi_years; `before` and
  `during` use a/b.
"""

GROUND_SYSTEM = """\
You check rendered prose against its sources.

supported   = the claim appears in a source fragment
inferred    = a reasonable reading of the sources but not stated in them
unsupported = not derivable from the sources at all

Names, dates, places, numbers and quoted speech must be `supported` or they are
`unsupported`. There is no middle verdict for a fact of that kind.
"""

_S = {"type": ["string", "null"]}
_N = {"type": ["number", "null"]}

EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "events": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "tmp_id": {"type": "string"},
                    "summary": {"type": "string"},
                    "granularity": {"type": "string",
                                    "enum": ["moment", "episode", "era"]},
                    "entities": {"type": "array", "items": {"type": "string"}},
                    "span": {"type": "string"},
                },
                "required": ["tmp_id", "summary", "granularity", "entities",
                             "span"],
                "additionalProperties": False,
            },
        },
        "constraints": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string",
                             "enum": ["absolute", "gap", "before", "during"]},
                    "of": _S, "from": _S, "to": _S, "a": _S, "b": _S,
                    "lo": _S, "hi": _S, "lo_years": _N, "hi_years": _N,
                },
                "required": ["kind", "of", "from", "to", "a", "b", "lo", "hi",
                             "lo_years", "hi_years"],
                "additionalProperties": False,
            },
        },
        "unresolved": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["events", "constraints", "unresolved"],
    "additionalProperties": False,
}

GROUND_SCHEMA = {
    "type": "object",
    "properties": {
        "sentences": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "ix": {"type": "integer"},
                    "verdict": {"type": "string",
                                "enum": ["supported", "inferred", "unsupported"]},
                    "evidence": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["ix", "verdict", "evidence"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["sentences"],
    "additionalProperties": False,
}


def _client(api_key: str | None = None):
    import anthropic
    if api_key:
        return anthropic.Anthropic(api_key=api_key)
    # Zero-arg: resolves ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN, or an
    # `ant auth login` profile, in that order.
    return anthropic.Anthropic()


def json_call(system: str, user: str, schema: dict,
              max_tokens: int = 16000, api_key: str | None = None) -> dict:
    """One structured call. `output_config.format` guarantees the response
    parses, so there is no fence-stripping and no retry-on-bad-JSON path.

    Note there is no `temperature`: it was removed on current models and
    sending it is a 400. Determinism for rendering comes from the render
    cache in render.py, not from sampling settings.
    """
    r = _client(api_key).messages.create(
        model=MODEL,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
        output_config={"format": {"type": "json_schema", "schema": schema}},
    )
    if r.stop_reason == "refusal":
        raise RuntimeError(
            f"model declined: {getattr(r.stop_details, 'category', None)}")
    text = next(b.text for b in r.content if b.type == "text")
    return json.loads(text)


# --------------------------------------------------------------- extraction

def extract(fragment_body: str, known_events: dict[str, str],
            api_key: str | None = None) -> dict:
    """known_events maps existing event id -> summary, so the model can attach
    new material to what is already there instead of duplicating it."""
    if backend(api_key) == "offline":
        return offline_extract(fragment_body, known_events)
    catalogue = "\n".join(f"- {k}: {v}" for k, v in known_events.items())
    return json_call(
        EXTRACT_SYSTEM,
        f"Existing events:\n{catalogue or '(none)'}\n\nText:\n{fragment_body}",
        EXTRACT_SCHEMA, api_key=api_key,
    )


_MONTHS = ("january february march april may june july august september "
           "october november december").split()
_STOP = {"i", "we", "my", "the", "a", "an", "it", "that", "this", "then",
         "after", "before", "when", "and", "but", "so", "he", "she", "they",
         "there", "here", "later", "earlier", "one", "two", "three"}

# Referring expressions the offline path should surface rather than swallow.
# The API extractor emits these itself; matching them here keeps the entity
# queue exercised on the offline path too.
_REFERRING = re.compile(
    r"\b((?:my|his|her|their|our)\s+"
    r"(?:wife|husband|mother|father|brother|sister|son|daughter|uncle|aunt|"
    r"cousin|friend|boss|neighbour|neighbor|teacher))\b", re.I)

_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_MON_YEAR = re.compile(r"\b(" + "|".join(_MONTHS) + r")\s+(\d{4})\b", re.I)
_YEAR = re.compile(r"\b(1[89]\d{2}|20\d{2})\b")
_REL = re.compile(
    r"\b(?:about\s+|around\s+|roughly\s+)?"
    r"(a couple of|a few|one|two|three|four|five|six|seven|eight|nine|ten|\d+)"
    r"\s+(year|month)s?\s+(later|earlier|before|after)\b", re.I)
# "two years later than the wedding" names its own anchor. Resolving which
# event that is needs the model; guessing the previous sentence would be
# exactly the fabrication the design forbids, so it goes to unresolved.
_HAS_REFERENT = re.compile(r"\b(?:than|after|before)\s+(?:the|my|our|his|her)\b",
                           re.I)
_WORDNUM = {"a couple of": 2, "a few": 3, "one": 1, "two": 2, "three": 3,
            "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
            "nine": 9, "ten": 10}


def _month_span(y: int, m: int) -> tuple[str, str]:
    import calendar
    return f"{y:04d}-{m:02d}-01", f"{y:04d}-{m:02d}-{calendar.monthrange(y, m)[1]:02d}"


def offline_extract(body: str, known_events: dict[str, str]) -> dict:
    """Rule-based fallback. One event per sentence, explicit dates only, and
    an honest `unresolved` for everything it cannot ground."""
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", body.strip())
                 if s.strip()]
    events, constraints, unresolved = [], [], []

    for i, sent in enumerate(sentences):
        tmp = f"t{i + 1}"
        n_unresolved_before = len(unresolved)
        words = re.findall(r"[\w']+", sent)
        # words[1:]: the first token is capitalised by orthography, not because
        # it names anyone.
        ents = {w for w in words[1:]
                if w[:1].isupper() and w.lower() not in _STOP
                and w.lower() not in _MONTHS}
        ents |= {m.group(1).lower() for m in _REFERRING.finditer(sent)}
        ents = sorted(ents)
        events.append({
            "tmp_id": tmp,
            "summary": _summarise(words),
            "granularity": "episode",
            "entities": ents,
            "span": sent,
        })

        placed = False
        if m := _ISO.search(sent):
            constraints.append(_c("absolute", of=tmp, lo=m.group(0), hi=m.group(0)))
            placed = True
        elif m := _MON_YEAR.search(sent):
            y, mon = int(m.group(2)), _MONTHS.index(m.group(1).lower()) + 1
            lo, hi = _month_span(y, mon)
            constraints.append(_c("absolute", of=tmp, lo=lo, hi=hi))
            placed = True
        elif m := _YEAR.search(sent):
            y = int(m.group(1))
            constraints.append(_c("absolute", of=tmp,
                                  lo=f"{y}-01-01", hi=f"{y}-12-31"))
            placed = True

        if m := _REL.search(sent):
            qty = _WORDNUM.get(m.group(1).lower())
            if qty is None:
                qty = float(m.group(1))
            yrs = qty if m.group(2).lower() == "year" else qty / 12.0
            lo_y, hi_y = yrs * 0.75, yrs * 1.25
            if _HAS_REFERENT.search(sent[m.end():]):
                unresolved.append(sent[m.start():].strip())
            elif i > 0:
                prev = f"t{i}"
                if m.group(3).lower() in ("later", "after"):
                    constraints.append(_c("gap", **{"from": prev, "to": tmp},
                                          lo_years=lo_y, hi_years=hi_y))
                else:
                    constraints.append(_c("gap", **{"from": tmp, "to": prev},
                                          lo_years=lo_y, hi_years=hi_y))
                placed = True
            else:
                unresolved.append(m.group(0))

        if not placed and len(unresolved) == n_unresolved_before:
            # Only when the sentence contributed nothing above: a phrase that
            # already went to the queue verbatim should not also be logged as
            # a bare "later".
            hits = re.findall(r"\b(?:later|earlier|afterwards?|before then|"
                              r"back then|that year|the year after|"
                              r"a while (?:later|before))\b", sent, re.I)
            unresolved.extend(hits or [])

    return {"events": events, "constraints": constraints,
            "unresolved": unresolved}


_DANGLING = {"the", "a", "an", "in", "on", "at", "of", "with", "about", "to",
             "for", "and", "but", "or", "my", "our", "his", "her", "we", "i",
             "was", "were", "that", "than", "from", "by", "it"}


def _summarise(words: list[str], limit: int = 8) -> str:
    """First few words, not ending mid-phrase. "an argument with Ravi about
    the" reads as truncation; "an argument with Ravi" reads as a summary."""
    out = words[:limit]
    while len(out) > 2 and out[-1].lower() in _DANGLING:
        out.pop()
    return " ".join(out)


def _c(kind: str, **kw) -> dict:
    """Constraint dict with every field present, matching EXTRACT_SCHEMA."""
    out = {"kind": kind, "of": None, "from": None, "to": None, "a": None,
           "b": None, "lo": None, "hi": None, "lo_years": None,
           "hi_years": None}
    out.update(kw)
    return out


# ------------------------------------------------------------------- landing

def validate_key(api_key: str) -> tuple[bool, str]:
    """Cheap check that a pasted key works, so a bad one fails at paste time
    rather than half way through a compile. Never echo the key back."""
    try:
        _client(api_key).models.list(limit=1)
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    return True, "ok"


def apply(store, out: dict, fragment_id: str) -> dict:
    """Land an extraction into the network *and* the store. Inconsistent
    constraints are recorded as conflicts rather than forced in.

    Returns a report: new event ids, how many constraints landed, what was
    rejected, and what went on the unresolved queue.
    """
    idmap: dict[str, str] = {}
    for e in out.get("events", []):
        ev = store.add_event(e["summary"], entities=e.get("entities", []),
                             granularity=e.get("granularity", "episode"),
                             from_fragment=fragment_id)
        idmap[e["tmp_id"]] = ev.id

    def rid(k):
        return idmap.get(k, k)

    tl = store.tl
    landed, rejected = 0, []
    for c in out.get("constraints", []):
        k = c.get("kind")
        try:
            if k == "absolute":
                ok = _absolute(store, rid(c["of"]), c["lo"], c["hi"], fragment_id)
            elif k == "gap":
                ok = _gap(store, rid(c["from"]), rid(c["to"]),
                          c["lo_years"], c["hi_years"], fragment_id)
            elif k == "before":
                ok = _before(store, rid(c["a"]), rid(c["b"]), fragment_id)
            elif k == "during":
                ok = _during(store, rid(c["a"]), rid(c["b"]), fragment_id)
            else:
                continue
        except (KeyError, TypeError, ValueError) as exc:
            rejected.append(f"{k}: bad reference ({exc})")
            continue
        if ok:
            landed += 1
        else:
            rejected.append(f"{k}: contradicts what is already known")

    for u in out.get("unresolved", []):
        store.add_unresolved(fragment_id, u)

    return {
        "events": list(idmap.values()),
        "landed": landed,
        "rejected": rejected,
        "unresolved": list(out.get("unresolved", [])),
        "needs_placement": [e for e in idmap.values()
                            if tl.slack(e) > 2 * YEAR],
    }


def _absolute(store, eid, lo, hi, src) -> bool:
    from timeline import days
    ev = store.tl.events[eid]
    a, _ = store.assert_constraint(ev.s, 0, days(lo), days(hi),
                                   PROV_ABSOLUTE, src, "stated date")
    b, _ = store.assert_constraint(ev.e, 0, days(lo), days(hi),
                                   PROV_ABSOLUTE, src, "stated date")
    return a and b


def _gap(store, a_id, b_id, lo_y, hi_y, src) -> bool:
    A, B = store.tl.events[a_id], store.tl.events[b_id]
    ok, _ = store.assert_constraint(B.s, A.e, lo_y * YEAR, hi_y * YEAR,
                                    PROV_STATED, src, "stated gap")
    return ok


def _before(store, a_id, b_id, src) -> bool:
    from timeline import INF
    A, B = store.tl.events[a_id], store.tl.events[b_id]
    ok, _ = store.assert_constraint(B.s, A.e, 1.0, INF,
                                    PROV_STATED, src, "stated order")
    return ok


def _during(store, a_id, b_id, src) -> bool:
    from timeline import INF
    A, B = store.tl.events[a_id], store.tl.events[b_id]
    ok1, _ = store.assert_constraint(A.s, B.s, 0.0, INF,
                                     PROV_STATED, src, "stated containment")
    ok2, _ = store.assert_constraint(B.e, A.e, 0.0, INF,
                                     PROV_STATED, src, "stated containment")
    return ok1 and ok2


# ------------------------------------------------------------------ grounding

def ground(paragraph: str, sources: dict[str, str],
           api_key: str | None = None) -> dict:
    """Run on every rendered paragraph before it is written to the manuscript.
    Strip `unsupported` sentences; mark `inferred` ones in the UI."""
    if backend(api_key) == "offline":
        return offline_ground(paragraph, sources)
    src = "\n\n".join(f"[{k}] {v}" for k, v in sources.items())
    return json_call(GROUND_SYSTEM, f"Sources:\n{src}\n\nProse:\n{paragraph}",
                     GROUND_SCHEMA, max_tokens=4000, api_key=api_key)


def sentences_of(paragraph: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", paragraph.strip())
            if s.strip()]


def offline_ground(paragraph: str, sources: dict[str, str]) -> dict:
    """Token-overlap check. Crude on purpose: it catches a sentence that shares
    almost nothing with its sources, which is the failure mode that matters,
    and stays silent otherwise."""
    def toks(s):
        return {w for w in re.findall(r"[a-z']+", s.lower()) if len(w) > 3}

    src_toks = {k: toks(v) for k, v in sources.items()}
    out = []
    for i, sent in enumerate(sentences_of(paragraph)):
        st = toks(sent)
        if not st:
            out.append({"ix": i, "verdict": "inferred", "evidence": []})
            continue
        ev = [k for k, v in src_toks.items() if len(st & v) / len(st) >= 0.5]
        if ev:
            verdict = "supported"
        elif any(st & v for v in src_toks.values()):
            verdict = "inferred"
            ev = [k for k, v in src_toks.items() if st & v]
        else:
            verdict = "unsupported"
        out.append({"ix": i, "verdict": verdict, "evidence": ev})
    return {"sentences": out}
