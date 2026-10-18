"""
Role separation for the R2-3 acceptance evaluation (review 2026-10-18).

The acceptance labels and the package are only trustworthy if the people who hold the incompatible roles are different people:

  labeler_1, labeler_2   label all 100 tickets independently, blind
  adjudicator            resolves disagreements between the labelers (so cannot be one of them)
  custodian              holds the private acceptance package before the freeze (so cannot be a labeler, who must not see it)
  golden_approver        approves the content of the 20 acceptance-only synthetic tickets (same reason)

No person may hold more than one of these five roles. The owner can sign a record with a name that is also a labeler's, so name similarity alone is never trusted in either direction:
two names that look alike (a shared word, or a one-or-two letter difference in a word) must be **declared** in the record as the same person or as different people, and a pair that is
declared the same person is one person. An undeclared look-alike pair, a missing role, or a person holding two roles is an error, and the freeze refuses it.
"""
from __future__ import annotations

import json
import re
from itertools import combinations
from pathlib import Path
from typing import Any, Dict, List, Optional

ROLES = ("labeler_1", "labeler_2", "adjudicator", "custodian", "golden_approver")
PLACEHOLDERS = {"", "owner", "admin", "test", "tbd", "todo", "none", "n/a", "name", "unknown", "x", "developer", "ai assistant", "claude"}
ROLES_PATH = Path(__file__).resolve().parent.parent / "docs" / "evaluation" / "roles.json"


class RoleError(Exception):
    pass


def _tokens(name: str) -> List[str]:
    return re.findall(r"[a-z]+", str(name).lower())


def _distance(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def looks_alike(a: str, b: str) -> bool:
    """True when two different names might be one person: they share a word, or a word of one is within two letters of a word of the other."""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return False
    if " ".join(ta) == " ".join(tb):
        return True
    return any(x == y or (min(len(x), len(y)) >= 4 and _distance(x, y) <= 2) for x in ta for y in tb)


def _norm(name: str) -> str:
    return " ".join(_tokens(name))


def _pair(a: str, b: str) -> frozenset:
    return frozenset((_norm(a), _norm(b)))


def check(record: Dict[str, Any]) -> List[str]:
    """The problems with a role record (empty when the separation holds).

    The people considered are the five role holders and the `owner` (who signed the Release 2 approval). Any two of them whose names look alike must be declared in
    `same_person` or `different_people`. Declared same-person pairs are joined transitively, so "the owner is Vijay" and "the owner is Ramanathan" together make the labeler
    Vijay and the adjudicator Ramanathan one person, which is an error."""
    problems: List[str] = []
    roles = record.get("roles") or {}
    holders: Dict[str, str] = {}
    for role in ROLES:
        name = str(roles.get(role) or "").strip()
        if not _tokens(name) or _norm(name) in PLACEHOLDERS:
            problems.append(f"{role}: no named person")
        else:
            holders[role] = name
    people: Dict[str, str] = {role: name for role, name in holders.items()}
    owner = str(record.get("owner") or "").strip()
    if _tokens(owner):
        people["owner"] = owner
    parent: Dict[str, str] = {k: k for k in people}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    declared_same = [_pair(*g[:2]) for g in record.get("same_person", []) if len(g) >= 2]
    declared_diff = {_pair(*g[:2]) for g in record.get("different_people", []) if len(g) >= 2}
    for a, b in combinations(sorted(people), 2):
        na, nb = people[a], people[b]
        pair = _pair(na, nb)
        if _norm(na) == _norm(nb) or pair in declared_same:
            parent[find(a)] = find(b)
        elif looks_alike(na, nb) and pair not in declared_diff:
            problems.append(f"{a} ({na}) and {b} ({nb}) look like the same person: declare them in `same_person` or `different_people`")
    for a, b in combinations(sorted(holders), 2):
        if find(a) == find(b):
            via = "" if _norm(holders[a]) == _norm(holders[b]) or _pair(holders[a], holders[b]) in declared_same else " (through the declared identities)"
            problems.append(f"{a} and {b} are the same person{via} ({holders[a]} / {holders[b]}); one person cannot hold both roles")
    author = str(record.get("golden_author") or "")
    if _tokens(author) and "golden_approver" in holders and _norm(author) == _norm(holders["golden_approver"]):
        problems.append("the golden approver is also the author of the goldens")
    return problems


def load(path: Optional[Path] = None) -> Dict[str, Any]:
    p = Path(path) if path else ROLES_PATH
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RoleError(f"the role record cannot be read: {exc}") from exc


def require(path: Optional[Path] = None) -> Dict[str, Any]:
    record = load(path)
    problems = check(record)
    if problems:
        raise RoleError("the roles are not separated: " + "; ".join(problems))
    return record
