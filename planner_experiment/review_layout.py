"""
The blinded review layout (design 13.5, "Blinded review").

For semantic grounding and unsupported-cause judgements a reviewer sees, per ticket, two outputs (baseline and candidate) in **one identical anonymised layout**: a cause list, evidence excerpts and an
escalation flag, in random order per ticket, with system identifiers, run ids, step traces, timings and format cues removed, and the same evidence packet for both. The assignment of outputs to
positions is kept in a separate key that is released only after the judgements are recorded. A blinding check (reviewers guess which output is which on a sample of calibration tickets) must be at most 60%.

`forbidden_cues` is the guard: a packet whose text contains a system marker (evidence ids like `e3`, names such as rules_only / baseline / candidate / investigation / hypothesis / Groq, word-forms such as
"supported" or "weak", timings or run ids) is refused, so a layout that leaks which side is which cannot be handed out.
"""
from __future__ import annotations

import hashlib
import random
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

LAYOUT_VERSION = "1"
MAX_CAUSES = 5
MAX_EVIDENCE = 5
EXCERPT_CHARS = 300
BLINDING_MAX_ACCURACY = 0.60
_PREFIXES = re.compile(r"^(?:recurrence of an earlier incident|same underlying issue as similar incident|probable cause|cause|hypothesis)\s*[:\-]\s*", re.I)
CUES = (
    (re.compile(r"\be\d+\b", re.I), "an evidence id"), (re.compile(r"\b(?:rules[_ ]only|rules[_ ]then[_ ]model|baseline|candidate|investigation|hypothes[ie]s|deterministic|agentic|loop)\b", re.I), "a system name"),
    (re.compile(r"\b(?:groq|gemma|llm|lm studio|planner|runner|tool call)\b", re.I), "a component name"), (re.compile(r"\b(?:supported|weak|verified|unverified)\b", re.I), "a status word"),
    (re.compile(r"\b\d+(?:\.\d+)?\s?(?:ms|s|sec|seconds|milliseconds)\b", re.I), "a timing"), (re.compile(r"\b(?:run|trace|request)[_ -]?id\b|\b[0-9a-f]{16,}\b", re.I), "an id"),
)


class LayoutError(Exception):
    pass


def forbidden_cues(text: str) -> List[str]:
    return [label for pattern, label in CUES if pattern.search(text or "")]


def _clean(text: Any, limit: int) -> str:
    t = " ".join(str(text or "").split())
    t = re.sub(r"\[REDACTED[^\]]*\]", "[removed]", t)                          # one neutral marker on both sides
    t = _PREFIXES.sub("", t).strip(" -*•\t")
    t = re.sub(r"\s+", " ", t)
    if t:
        t = t[0].upper() + t[1:]
    t = t.rstrip(" .;,") + ("." if t else "")
    return t if len(t) <= limit else t[: limit - 1].rstrip() + "…"


def normalise_output(causes: Sequence[str], evidence: Sequence[str], escalate: bool) -> Dict[str, Any]:
    """The common form: capitalised sentences without prefixes or markers, at most five causes and five evidence excerpts of at most 300 characters, evidence sorted so order carries no cue."""
    cs = [c for c in (_clean(c, 200) for c in causes) if c][:MAX_CAUSES]
    ev = sorted({e for e in (_clean(e, EXCERPT_CHARS) for e in evidence) if e})[:MAX_EVIDENCE]
    return {"causes": cs, "evidence": ev, "escalate": bool(escalate)}


def _render(output: Dict[str, Any]) -> str:
    lines = ["Probable causes:"] + ([f"  {i}. {c}" for i, c in enumerate(output["causes"], 1)] or ["  (none stated)"])
    lines += ["Evidence:"] + ([f"  - {e}" for e in output["evidence"]] or ["  (none cited)"])
    lines += [f"Escalate to a person: {'yes' if output['escalate'] else 'no'}"]
    return "\n".join(lines)


def build_packets(items: Sequence[Dict[str, Any]], seed: int) -> Tuple[List[Dict[str, Any]], Dict[str, Dict[str, str]]]:
    """items: [{"case_id", "evidence_packet": [str], "baseline": {causes, evidence, escalate}, "candidate": {...}}].

    Returns (packets, key). A packet is {"ticket": "T-014", "evidence_packet": [...], "outputs": {"A": text, "B": text}}: tickets in a seeded random order under neutral aliases, outputs in a seeded random
    order per ticket. The key {alias: {"A": "baseline"|"candidate", "B": ...}} is returned separately and must not reach the reviewers before their judgements are recorded."""
    rng = random.Random(seed)
    order = list(range(len(items)))
    rng.shuffle(order)
    packets: List[Dict[str, Any]] = []
    key: Dict[str, Dict[str, str]] = {}
    for position, index in enumerate(order, 1):
        item = items[index]
        alias = f"T-{position:03d}"
        sides = ["baseline", "candidate"]
        rng.shuffle(sides)
        normalised = {s: normalise_output(item[s].get("causes", []), item[s].get("evidence", []), item[s].get("escalate", False)) for s in ("baseline", "candidate")}
        outputs = {"A": _render(normalised[sides[0]]), "B": _render(normalised[sides[1]])}
        for slot, text in outputs.items():
            cues = forbidden_cues(text)
            if cues:
                raise LayoutError(f"{item['case_id']}: output {slot} would reveal its source ({', '.join(cues)}); normalise the content before it is handed out")
        packets.append({"ticket": alias, "evidence_packet": [str(e) for e in item.get("evidence_packet", [])], "outputs": outputs})
        key[alias] = {"A": sides[0], "B": sides[1], "case_id": item["case_id"]}
    return packets, key


_SHAPE = re.compile(r"Probable causes:\n(?:  .+\n)+Evidence:\n(?:  .+\n)+Escalate to a person: (?:yes|no)")


def layout_is_identical(packets: Sequence[Dict[str, Any]]) -> bool:
    """Both outputs of every packet have exactly the same sections in the same order (a format cue would be a difference here)."""
    return all(_SHAPE.fullmatch(p["outputs"][slot]) is not None for p in packets for slot in ("A", "B"))


def unblind(judgements: Dict[str, Dict[str, Any]], key: Dict[str, Dict[str, str]]) -> Dict[str, Dict[str, Any]]:
    """judgements: {alias: {"A": {...}, "B": {...}}} -> {case_id: {"baseline": {...}, "candidate": {...}}}. Every alias in the key must have judgements for both slots."""
    out: Dict[str, Dict[str, Any]] = {}
    for alias, mapping in key.items():
        if alias not in judgements or set(judgements[alias]) != {"A", "B"}:
            raise LayoutError(f"{alias}: judgements for both outputs are required before unblinding")
        out[mapping["case_id"]] = {mapping["A"]: judgements[alias]["A"], mapping["B"]: judgements[alias]["B"]}
    return out


def blinding_accuracy(guesses: Dict[str, str], key: Dict[str, Dict[str, str]]) -> Dict[str, Any]:
    """guesses: {alias: "A" | "B"} = which slot the reviewer believes is the candidate. Returns accuracy and whether it is near chance (<= 60%)."""
    if not guesses:
        raise LayoutError("no guesses")
    right = sum(1 for alias, slot in guesses.items() if key[alias][slot] == "candidate")
    accuracy = right / len(guesses)
    return {"n": len(guesses), "correct": right, "accuracy": round(accuracy, 3), "near_chance": accuracy <= BLINDING_MAX_ACCURACY}


def packet_hash(packets: Sequence[Dict[str, Any]]) -> str:
    import json
    return hashlib.sha256(json.dumps(list(packets), sort_keys=True, ensure_ascii=True).encode("utf-8")).hexdigest()
