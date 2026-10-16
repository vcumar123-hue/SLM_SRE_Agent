"""
rules_only: a deterministic planner with no model (design 9.2).

The same state always gives the same action. It asks only the registered read-only tools (the runner enforces the allow-list, budgets,
tenant and sanitising), and every cause it proposes is taken from the text or facts of an evidence item it cites; it invents nothing and
has no model fallback. When the evidence does not support a cause it finishes with weak hypotheses or escalates.

Rule table, in priority order:
  1. the ticket names a host            -> ticket_history_lookup(host)         (once)
  2. no incident search yet             -> incident_kb_search(ticket text)     (once)
  3. best retrieval score is low        -> incident_kb_search(reformulated)    (at most `max_reformulations`)
  4. a classification is known          -> outcome_lookup, then pattern_lookup
  5. two leading classifications tie    -> evidence_lookup                     (once), and the run is reported as a conflict
  6. otherwise                          -> finish (or escalate when nothing relevant was found)
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from agentruntime.contracts import Action
from agentruntime.runner import PlannerReply

MIN_RETRIEVAL_SCORE = 0.25      # the product's own base retrieval threshold
CONFLICT_GAP = 0.05             # two leading classifications this close in score, and both relevant, are a conflict
MIN_PATTERN_RESOLUTIONS = 3     # a pattern backed by fewer resolutions is only weak support
QUERY_CHARS = 300               # the natural query: the opening of the ticket text
HISTORY_DAYS = 400
MAX_HYPOTHESES = 3
CAUSE_CHARS = 160

# tokens that look like a host or instance id: letters and digits, not an error or record code
_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9]*\d[A-Za-z0-9]*|\d+[A-Za-z][A-Za-z0-9]*")
_CODE_SHAPES = (
    re.compile(r"^(?:RC|SQL|ORA|HTTP|STC|SQLCODE)\d+$", re.I),                    # short return and error codes (RC1, SQL0204)
    re.compile(r"^[A-Za-z]{1,5}\d{5,}$"),                                         # record ids, return codes, message ids (INC3313708, PC002680, P1608697)
    re.compile(r"^[0-9a-f]{4,}$", re.I),                                           # hex fragments of ids and hashes (a01e, fa1b1e09)
    re.compile(r"^\d+(?:st|nd|rd|th)$", re.I),
)
_UUID = re.compile(r"(?<![0-9a-z])[0-9a-f]{4,8}(?:-[0-9a-f]{2,12}){2,}", re.I)         # whole or truncated UUIDs: their pieces are not hosts
_CLASSIFICATION = re.compile(r"[a-z0-9_]{2,80}")
_NO_CLASSIFICATION = {"unknown", "unclassified", "none", "other", "general"}      # retrieval's placeholder labels are not classifications

# causes the text of an earlier incident states about itself
_ROOT_CAUSE = re.compile(r"root cause:\s*(.+?)(?=\s+(?:resolution|validation|actions?|next steps?):|$)", re.I | re.S)
_ASSOCIATED = re.compile(r"(?:commonly associated with|possible causes include|likely causes include|typically caused by)\s+(.+?)(?:\.\s|\.$|$)", re.I | re.S)


def extract_host(text: str) -> Optional[str]:
    """The first host-like token in the ticket text: a letters-and-digits token, taken from a hyphen/underscore/dot separated name."""
    for word in re.split(r"[\s,;:()\[\]<>\"'=]+", _UUID.sub(" ", text or "")):
        for part in re.split(r"[-_./\\]+", word):
            if len(part) < 3 or len(part) > 40 or not _TOKEN.fullmatch(part):
                continue
            if any(shape.match(part) for shape in _CODE_SHAPES):
                continue
            return part.lower()
    return None


def reformulate(text: str, host: Optional[str]) -> str:
    """The ticket's descriptive words only: identifiers, codes and numbers are removed so the search is about the symptom."""
    words = []
    for word in re.split(r"[\s,;:()\[\]<>\"'=]+", text or ""):
        stripped = word.strip("-_./\\")
        if len(stripped) < 3 or any(ch.isdigit() for ch in stripped) or (host and host in stripped.lower()):
            continue
        words.append(stripped)
    return " ".join(words)[:QUERY_CHARS]


def classification_of(facts: Optional[Dict[str, Any]]) -> Optional[str]:
    raw = str((facts or {}).get("classification") or "").strip().lower()
    raw = re.sub(r"[^a-z0-9_]+", "_", raw).strip("_")
    return raw if _CLASSIFICATION.fullmatch(raw) and raw not in _NO_CLASSIFICATION else None


def _real(item) -> bool:
    """Evidence that carries information (a replayed 'no recorded output' placeholder does not)."""
    return (item.facts or {}).get("recorded") is not False


def _norm_cause(text: str) -> str:
    return re.sub(r"^(?:and|or)\s+", "", re.sub(r"\s+", " ", text).strip(" .;,")).lower()


def causes_in(excerpt: str) -> List[str]:
    """The causes an incident's own text states (its 'Root Cause:' clauses, or 'commonly associated with / possible causes include' lists),
    normalised so the same cause in different incidents compares equal. Nothing is inferred: a phrase is returned only if it is in the text."""
    found: List[str] = []
    for match in _ROOT_CAUSE.finditer(excerpt or ""):
        found += [_norm_cause(part) for part in match.group(1).split(";")]
    for match in _ASSOCIATED.finditer(excerpt or ""):
        found += [_norm_cause(part) for part in re.split(r",|\s+and\s+|\s+or\s+", match.group(1))]
    seen: List[str] = []
    for cause in found:
        if 3 <= len(cause) <= CAUSE_CHARS and cause not in seen:
            seen.append(cause)
    return seen


def _first_sentence(excerpt: str) -> str:
    head = re.split(r"(?<=[a-z0-9\)])\.\s", excerpt, maxsplit=1)[0].strip(" .")
    return head[:CAUSE_CHARS]


class RulesOnlyPlanner:
    prompt_version = "none"
    ruleset_version = "rules-1"
    model_id = None

    def __init__(self, case: Optional[Dict[str, Any]] = None) -> None:
        self.case = case

    # -- the decision ----------------------------------------------------------------------------------------------------

    def next_action(self, state) -> PlannerReply:
        return PlannerReply(self._decide(state))

    def decide(self, state) -> Action:
        """The rules' action for this state (used by rules_then_model, which consults the model only when the rules have nothing more to ask)."""
        return self._decide(state)

    def _decide(self, state) -> Action:
        text = str(state.task.get("ticket_text") or "")
        budgets = state.policy.budgets
        calls = [(s.tool, s.tool_input or {}) for s in state.steps if s.kind == "tool" and s.tool and not s.skipped]
        called = {tool for tool, _ in calls}
        queries = [args.get("query") for tool, args in calls if tool == "incident_kb_search"]
        calls_left = int(budgets.max_tool_calls) - int(state.counters.get("tool_calls", 0))
        host = extract_host(text)
        kb = [e for e in state.evidence if e.tool == "incident_kb_search" and _real(e)]
        ranked = sorted(kb, key=lambda e: -(e.relevance or 0.0))
        best = ranked[0].relevance if ranked else None

        def ask(tool: str, **args: Any) -> Action:
            return Action(kind="tool", tool=tool, input=args, reason=f"rule: {tool}")

        if calls_left > 0:
            if host and "ticket_history_lookup" not in called:
                return ask("ticket_history_lookup", host=host, days=HISTORY_DAYS)
            if "incident_kb_search" not in called:
                return ask("incident_kb_search", query=(text[:QUERY_CHARS] or "incident"), top_k=5)
            if (best is None or best < MIN_RETRIEVAL_SCORE) and len(queries) < 1 + int(budgets.max_reformulations):
                alt = reformulate(text, host)
                if len(alt) >= 3 and alt not in queries:
                    return ask("incident_kb_search", query=alt, top_k=5)
            classification = self._classification(ranked)
            if classification:
                if "outcome_lookup" not in called:
                    return ask("outcome_lookup", classification=classification)
                if "pattern_lookup" not in called:
                    return ask("pattern_lookup", classification=classification)
            if self._conflict(ranked) and "evidence_lookup" not in called:
                return ask("evidence_lookup")
        return self._conclude(state, ranked)

    # -- helpers ---------------------------------------------------------------------------------------------------------

    @staticmethod
    def _classification(ranked) -> Optional[str]:
        for item in ranked:
            if (item.relevance or 0.0) >= MIN_RETRIEVAL_SCORE:
                name = classification_of(item.facts)
                if name:
                    return name
        return None

    @staticmethod
    def _conflict(ranked) -> bool:
        relevant = [(e.relevance or 0.0, classification_of(e.facts)) for e in ranked if (e.relevance or 0.0) >= MIN_RETRIEVAL_SCORE]
        relevant = [(score, name) for score, name in relevant if name]
        if len(relevant) < 2:
            return False
        top_score, top_name = relevant[0]
        return any(name != top_name and top_score - score < CONFLICT_GAP for score, name in relevant[1:])

    def _conclude(self, state, ranked) -> Action:
        hypotheses: List[Dict[str, Any]] = []
        ids_by_class: Dict[str, List[str]] = {}
        for item in ranked:
            name = classification_of(item.facts)
            if name and (item.relevance or 0.0) >= MIN_RETRIEVAL_SCORE:
                ids_by_class.setdefault(name, []).append(item.evidence_id)

        # 1. causes the client's own resolution patterns record for the classification (cited with the incidents of that classification)
        for item in state.evidence:
            if item.tool != "pattern_lookup" or not _real(item):
                continue
            facts = item.facts or {}
            resolutions = int(facts.get("total_resolutions") or 0)
            strong = resolutions >= MIN_PATTERN_RESOLUTIONS and str(facts.get("pattern_confidence") or "").lower() in ("high", "medium")
            for cause in (facts.get("dominant_root_causes") or [])[:MAX_HYPOTHESES]:
                hypotheses.append({"cause": str(cause)[:CAUSE_CHARS], "status": "supported" if strong else "weak",
                                   "evidence": [item.evidence_id] + ids_by_class.get(item.source_id, [])[:2],
                                   "notes": f"recorded in {resolutions} earlier resolution(s) of {item.source_id}"})

        # 2. causes the retrieved incidents state in their own text; a cause stated by two or more relevant incidents is supported,
        #    one stated by a single incident is weak. Each is cited with the incidents that state it.
        relevant = [e for e in ranked if (e.relevance or 0.0) >= MIN_RETRIEVAL_SCORE]
        stated: Dict[str, List[str]] = {}
        for item in relevant:
            for cause in causes_in(item.excerpt):
                stated.setdefault(cause, []).append(item.evidence_id)
        for cause, ids in sorted(stated.items(), key=lambda kv: -len(kv[1])):
            if cause in {h["cause"] for h in hypotheses}:
                continue
            hypotheses.append({"cause": cause, "status": "supported" if len(ids) >= 2 else "weak", "evidence": ids[:3],
                               "notes": f"stated by {len(ids)} of {len(relevant)} similar incident(s)"})

        # 3. nothing states a cause: the closest earlier incident is cited as a recurrence only, never as supported
        if not hypotheses and relevant:
            seen_labels = set()
            for item in relevant:
                label = _first_sentence(item.excerpt)
                key = re.sub(r"\d+", "#", label.lower())
                if label and key not in seen_labels:
                    seen_labels.add(key)
                    same = [e.evidence_id for e in relevant if re.sub(r"\d+", "#", _first_sentence(e.excerpt).lower()) == key]
                    hypotheses.append({"cause": f"recurrence of an earlier incident: {label}", "status": "weak", "evidence": same[:3],
                                       "notes": f"closest retrieval score {item.relevance or 0.0:.2f}; no cause is recorded in the incident text"})
                if len(seen_labels) >= 2:
                    break

        # 4. history on the same host is context for a recurrence, never a cause by itself
        history = [e for e in state.evidence if e.tool == "ticket_history_lookup" and _real(e)]
        if len(history) >= 2:
            label = _first_sentence(history[0].excerpt.split(" | ")[0])
            hypotheses.append({"cause": f"recurring problem on this host: {len(history)} earlier tickets, the latest '{label}'", "status": "weak",
                               "evidence": [e.evidence_id for e in history[:3]], "notes": "recurrence only; the cause is not recorded"})

        hypotheses = sorted(hypotheses, key=lambda h: h["status"] != "supported")[:MAX_HYPOTHESES]
        if not hypotheses:
            return Action(kind="escalate", reason="no relevant earlier incident, pattern or history was found for this ticket")
        causes = [h["cause"] for h in hypotheses if h["status"] == "supported"]
        actions: List[str] = []
        for item in state.evidence:
            if item.tool == "pattern_lookup" and _real(item):
                actions += [str(a) for a in ((item.facts or {}).get("dominant_actions") or [])]
        analysis = {"probable_causes": causes, "next_checks": actions[:3]} if causes else None
        return Action(kind="finish", hypotheses=hypotheses, analysis=analysis, conflict=self._conflict(ranked),
                      reason="rule: finish with the cited evidence")
