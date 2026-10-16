"""
Held-out exclusion (design 13.2): a held-out ticket, its text and its cluster must not be retrievable by the planner being evaluated, or the loop could
"find the answer by finding the ticket itself".

`Matcher` decides, for one piece of tool output, whether it is a held-out ticket, a near-copy of one, or in the cluster of one. It is conservative: when a
decision cannot be made it says so (`Undecidable`) and the capture refuses, because an unfiltered result must never be recorded silently.

What is matched (all within the same tenant; a held-out ticket never hides another tenant's evidence, and another tenant's evidence is rejected by the
capture before it gets here):
  * ticket_id  the held-out ticket's id appears in the item's id or text (whole token, any case);
  * text       the item's title equals the held-out text after case/punctuation folding, or the held-out text (four or more words) is contained in the item,
               or at least 90% of the held-out text's words (four or more) appear in the item's title;
  * cluster    the item's cluster id (same definition as the draw: tenant, host, first eight words with hosts and numbers folded) equals the held-out
               ticket's. When the item has no derivable host the alert-text pattern alone is used (fail closed: it may exclude a little more, never less).
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional

from planner_experiment import select_cases as sc

EXCLUSION_VERSION = "1"
NEAR_TEXT = 0.9
MIN_WORDS = 4
HOST = re.compile(r"\b[a-z]{2,}[a-z0-9]*\d+[a-z0-9._-]*\b", re.I)        # the draw's host rule (cli.load_tickets)
_WORDS = re.compile(r"[a-z0-9#]+")


class Undecidable(Exception):
    """An item could not be classified; the capture treats this as a failure, never as 'keep'."""


def flat(text: Any) -> str:
    return " ".join(_WORDS.findall(str(text or "").lower()))


def words(text: Any) -> List[str]:
    return _WORDS.findall(str(text or "").lower())


def title_of(excerpt: str) -> str:
    """The item's short description: the text before the first ' | ' or sentence break, which is what the ticket text is compared with."""
    head = re.split(r"\s\|\s|(?<=[A-Za-z0-9\)\]])\.\s", str(excerpt or ""), maxsplit=1)[0]
    return head.strip(" .")[:300]


def host_of(text: str) -> str:
    m = HOST.search(text or "")
    return m.group(0).lower() if m else ""


def _pattern(text: str) -> str:
    return " ".join(sc.normalise(text).split()[:8])


class Matcher:
    def __init__(self, held_out: Iterable[Dict[str, Any]]) -> None:
        self.tickets: List[Dict[str, Any]] = []
        for t in held_out:
            tenant = str(t.get("tenant_id") or "").strip().lower()
            text = str(t.get("text") if t.get("text") is not None else t.get("ticket_text") or "")
            if not tenant or not flat(text):
                raise Undecidable(f"a held-out ticket ({t.get('ticket_id')!r}) has no tenant or no text, so it cannot be excluded reliably")
            pool = str(t.get("pool") or tenant)
            host = str(t.get("host") or "")
            ticket = {"pool": pool, "tenant_id": tenant, "text": text, "host": host}
            tid = str(t.get("ticket_id") or "")
            self.tickets.append({
                "tenant": tenant, "id": tid, "id_re": re.compile(rf"(?<![A-Za-z0-9]){re.escape(tid)}(?![A-Za-z0-9])", re.I) if tid else None,
                "flat": flat(text), "words": set(words(text)), "n_words": len(words(text)), "pool": pool,
                "cluster": sc.cluster_id(ticket), "pattern": _pattern(text),
            })
        if not self.tickets:
            raise Undecidable("there are no held-out tickets: exclusion cannot be proven for an empty set")

    # -- decision --------------------------------------------------------------------------------------------------------

    def reason(self, tenant: str, source_id: str, excerpt: str) -> Optional[str]:
        """Why this item must be removed ('ticket_id', 'text' or 'cluster'), or None when it may be kept."""
        title = title_of(excerpt)
        if not str(source_id or "").strip() or not flat(excerpt):
            raise Undecidable("an evidence item has no id or no text, so it cannot be checked against the held-out tickets")
        tenant = str(tenant or "").strip().lower()
        haystack = f"{source_id} {excerpt}"
        item_flat, title_flat = flat(excerpt), flat(title)
        title_words = set(words(title))
        item_host = host_of(title)
        for h in self.tickets:
            if h["tenant"] != tenant:
                continue
            if h["id_re"] is not None and h["id_re"].search(haystack):
                return "ticket_id"
            if title_flat == h["flat"] or (h["n_words"] >= MIN_WORDS and h["flat"] in item_flat):
                return "text"
            if h["n_words"] >= MIN_WORDS and title_words and len(h["words"] & title_words) / len(h["words"]) >= NEAR_TEXT and len(title_words) <= 2 * len(h["words"]):
                return "text"
            candidate = {"pool": h["pool"], "tenant_id": tenant, "text": title, "host": item_host}
            if item_host:
                if sc.cluster_id(candidate) == h["cluster"]:
                    return "cluster"
            elif _pattern(title) == h["pattern"]:
                return "cluster"
        return None

    def self_test(self) -> None:
        """Every held-out ticket, presented as evidence, must be removed. A matcher that fails this is broken, and nothing may be captured with it."""
        for h in self.tickets:
            text = " ".join(h["flat"].split())
            if not text:
                continue
            reasons = self.reason(h["tenant"], "selftest", f"{text}. selftest body")
            if reasons is None:
                raise Undecidable("the exclusion self-test failed: a held-out ticket's own text was not recognised")

    def leaks(self, tenant: str, source_id: str, excerpt: str) -> bool:
        return self.reason(tenant, source_id, excerpt) is not None
