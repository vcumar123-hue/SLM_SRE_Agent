"""Role separation for the R2-3 acceptance evaluation, and the separate reporting of goldens (amendment A-1)."""
import json
import pathlib
import tempfile
import unittest

from planner_experiment import evaluate_release2 as ev
from planner_experiment import roles
from tests.test_evaluate_release2 import REPLAY, SAFE, good_set, rec


def record(**over):
    r = {"roles": {"labeler_1": "Vijay", "labeler_2": "Sara", "adjudicator": "Ramanathan", "custodian": "Priya Nair", "golden_approver": "Tom Okafor"},
         "owner": "Vijay Ramanathan", "golden_author": "the developer (AI assistant)", "same_person": [],
         "different_people": [["Vijay Ramanathan", "Vijay"], ["Vijay Ramanathan", "Ramanathan"], ["Vijay", "Ramanathan"]]}
    r.update(over)
    return r


class RoleSeparationTests(unittest.TestCase):
    def test_five_different_people_pass(self):
        self.assertEqual(roles.check(record()), [])

    def test_one_person_cannot_hold_two_roles(self):
        for role, name in (("adjudicator", "Vijay"), ("custodian", "Sara"), ("golden_approver", "Vijay"), ("custodian", "vijay"), ("golden_approver", "Sara"), ("adjudicator", "Sara")):
            r = record()
            r["roles"][role] = name
            self.assertTrue(any("same person" in p for p in roles.check(r)), (role, name))
        r = record()
        r["roles"]["labeler_2"] = "Vijay"
        self.assertTrue(any("labeler_1 and labeler_2" in p for p in roles.check(r)))

    def test_the_adjudicator_the_custodian_and_the_golden_approver_may_be_one_person_with_a_warning(self):
        r = record()
        r["roles"].update(custodian="Ramanathan", golden_approver="Ramanathan")
        self.assertEqual(roles.check(r), [])
        self.assertEqual(len(roles.warnings(r)), 3)
        r["roles"].update(custodian="Priya Nair", golden_approver="Tom Okafor")
        self.assertEqual(roles.warnings(r), [])

    def test_the_same_name_in_a_different_spelling_or_case_is_the_same_person(self):
        r = record()
        r["roles"]["custodian"] = "  vijay "
        self.assertTrue(any("same person" in p for p in roles.check(r)))

    def test_look_alike_names_must_be_declared_never_guessed(self):
        problems = roles.check(record(different_people=[]))
        self.assertTrue(any("look like the same person" in p and "Vijay Ramanathan" in p for p in problems), problems)
        self.assertTrue(any("owner" in p and "adjudicator" in p for p in problems), problems)
        self.assertEqual(roles.check(record()), [])
        reordered = record(different_people=[["Vijay", "Vijay Ramanathan"], ["Ramanathan", "Vijay Ramanathan"], ["Ramanathan", "Vijay"]])
        self.assertEqual(roles.check(reordered), [])                                                # the order of a declared pair does not matter

    def test_a_pair_declared_the_same_person_is_one_person(self):
        r = record(same_person=[["Vijay", "Ramanathan"]])
        r["different_people"] = [["Vijay Ramanathan", "Vijay"], ["Vijay Ramanathan", "Ramanathan"]]
        problems = roles.check(r)
        self.assertTrue(any("labeler_1" in p and "adjudicator" in p and "same person" in p for p in problems), problems)

    def test_the_owner_being_both_the_labeler_and_the_adjudicator_is_caught_through_the_owner(self):
        r = record(same_person=[["Vijay Ramanathan", "Vijay"], ["Vijay Ramanathan", "Ramanathan"]], different_people=[["Vijay", "Ramanathan"]])
        problems = roles.check(r)
        self.assertTrue(any("(through the declared identities)" in p for p in problems), problems)

    def test_the_owner_being_a_labeler_alone_is_not_a_role_conflict_but_is_declared(self):
        r = record(same_person=[["Vijay Ramanathan", "Vijay"]], different_people=[["Vijay Ramanathan", "Ramanathan"], ["Vijay", "Ramanathan"]])
        self.assertEqual(roles.check(r), [])                                                        # the owner is also labeler 1: allowed, as long as no incompatible role is shared

    def test_ramanthan_and_ramanathan_look_alike(self):
        self.assertTrue(roles.looks_alike("Ramanthan", "Ramanathan"))
        self.assertTrue(roles.looks_alike("Vijay", "Vijay Ramanathan"))
        self.assertFalse(roles.looks_alike("Sara", "Tom Okafor"))
        self.assertFalse(roles.looks_alike("", "Sara"))

    def test_missing_or_placeholder_holders_are_errors(self):
        for name in ("", "  ", "owner", "TBD", "developer", "Claude"):
            r = record()
            r["roles"]["custodian"] = name
            self.assertTrue(any("custodian: no named person" in p for p in roles.check(r)), name)
        r = record()
        del r["roles"]["golden_approver"]
        self.assertTrue(any("golden_approver" in p for p in roles.check(r)))

    def test_the_golden_approver_may_not_be_the_author(self):
        r = record(golden_author="Tom Okafor")
        self.assertTrue(any("author" in p for p in roles.check(r)))

    def test_require_raises_with_every_problem(self):
        path = pathlib.Path(tempfile.mkdtemp()) / "roles.json"
        path.write_text(json.dumps(record(different_people=[])), encoding="utf-8")
        with self.assertRaisesRegex(roles.RoleError, "not separated"):
            roles.require(path)
        path.write_text(json.dumps(record()), encoding="utf-8")
        self.assertEqual(roles.require(path)["roles"]["custodian"], "Priya Nair")
        with self.assertRaises(roles.RoleError):
            roles.load(path.with_name("missing.json"))

    def test_the_repository_record_states_only_what_the_owner_has_said(self):
        repo = roles.load()
        self.assertEqual(repo["roles"]["adjudicator"], "Ramanathan")
        self.assertEqual((repo["roles"]["labeler_1"], repo["roles"]["labeler_2"]), ("Vijay", "Sara"))
        self.assertEqual((repo["roles"]["custodian"], repo["roles"]["golden_approver"]), ("", ""))     # not guessed
        self.assertEqual(repo["owner"], "Vijay Ramanathan")
        self.assertTrue(roles.check(repo), "until valid holders are named and the identities declared, the roles are not separated")
        self.assertIn(["Vijay", "Ramanathan"], repo["different_people"])                               # stated by the owner 2026-10-18
        self.assertEqual(repo["stated_but_not_valid"]["custodian"]["name"], "Vijay")                   # recorded as stated, not accepted: a labeler cannot be the custodian
        self.assertEqual(repo["stated_but_not_valid"]["golden_approver"]["name"], "Sara")


class GoldenReportingTests(unittest.TestCase):
    def mixed(self):
        records = good_set(80, not_det=16)
        for r in records:
            r["source"] = "real"
        goldens = [rec(100 + i, "not_determinable" if i < 4 else "determinable",
                       cand=({"outcome": "escalated_insufficient", "grounded": False, "escalated": True} if i < 4 else {})) for i in range(20)]
        for g in goldens:
            g["source"] = "golden"
        return records + goldens

    def evaluate_it(self, records):
        return ev.evaluate(records, SAFE, REPLAY, seed=3, resamples=300)

    def test_real_only_and_golden_only_results_are_reported_separately_and_not_gating(self):
        report = self.evaluate_it(self.mixed())
        self.assertEqual(report["tickets"], 100)
        self.assertEqual(set(report["subsets"]) - {"note"}, {"real_only", "golden_only"})
        self.assertEqual(report["subsets"]["real_only"]["tickets"], 80)
        self.assertEqual(report["subsets"]["golden_only"]["tickets"], 20)
        self.assertEqual(set(report["subsets"]["real_only"]["gates"]), {"G1", "G2", "G3", "G6"})

    def test_the_sensitivity_excluding_all_goldens_is_the_real_only_result(self):
        report = self.evaluate_it(self.mixed())
        self.assertEqual(report["sensitivity_excluding_goldens"], report["subsets"]["real_only"])

    def test_a_pass_that_depends_on_the_goldens_is_visible(self):
        records = self.mixed()
        for r in records:
            if r["source"] == "real":
                r["candidate"].update(grounded=False)                                            # the candidate is never grounded on real tickets...
                r["baseline"].update(grounded=False)
        report = self.evaluate_it(records)
        self.assertTrue(report["gates"]["G1"]["passed"])                                         # ...yet the pooled G1 passes on the goldens alone
        self.assertEqual(report["subsets"]["real_only"]["gates"]["G1"]["delta_points"], 0.0)       # and the real-only result shows it

    def test_the_gating_result_is_unchanged_by_the_subsets(self):
        mixed = self.mixed()
        plain = [dict(r) for r in mixed]
        for r in plain:
            r.pop("source")
        a, b = self.evaluate_it(mixed), self.evaluate_it(plain)
        self.assertEqual(a["gates"], b["gates"])
        self.assertNotIn("subsets", b)

    def test_an_unknown_source_is_refused(self):
        bad = rec(1)
        bad["source"] = "other"
        with self.assertRaises(ev.EvaluationError):
            ev.evaluate([bad], SAFE, REPLAY, 1, 10)


if __name__ == "__main__":
    unittest.main()
