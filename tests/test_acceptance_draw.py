"""The seeded private draw of the R2-3 acceptance set's real tickets (design 13.1, 13.2)."""
import json
import pathlib
import tempfile
import unittest

from planner_experiment import acceptance, seed as seed_mod
from planner_experiment import select_cases as sc
from tests.pe_world import KINDS, PARTITION_ARTIFACT

ARTIFACT = "c" * 40


def tickets(n=600):
    out = []
    for pool, wording in (("client_c", "problem reported on"), ("client_a", "fault noted against")):
        for i in range(n):
            out.append({"pool": pool, "tenant_id": pool, "ticket_id": f"INC{i:07d}", "text": f"{KINDS[i % len(KINDS)]} {wording} host{i}", "host": f"host{i}",
                        "opened_at": f"2026-{1 + i % 9:02d}-10 08:00:00"})
    out += [{"pool": "client_c", "tenant_id": "client_c", "ticket_id": f"CHG{i:07d}", "text": f"change {i}", "host": f"chg{i}", "opened_at": "2026-03-01 00:00:00"} for i in range(50)]
    out += [{"pool": "golden", "tenant_id": "poc_client", "ticket_id": f"golden-{i}", "text": f"golden incident {i}", "host": f"g{i}", "opened_at": "2026-04-01 00:00:00"} for i in range(5)]
    return out


class Records(unittest.TestCase):
    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp(prefix="acc-"))
        self.partition = self.dir / "seed_calibration.json"
        self.draw = self.dir / "seed_acceptance.json"
        seed_mod.write_record(self.partition, PARTITION_ARTIFACT, "calibration-selection")
        seed_mod.write_record(self.draw, ARTIFACT, "acceptance-selection")
        self.partition_seed = seed_mod.load_record(self.partition, "calibration-selection")["seed"]
        self.draw_seed = seed_mod.load_record(self.draw, "acceptance-selection")["seed"]


class SelectionTests(Records):
    def test_80_real_tickets_40_per_client_from_acceptance_buckets_only(self):
        res = acceptance.select_acceptance(tickets(), self.partition_seed, self.draw_seed)
        self.assertEqual(res["shortfall"], {})
        self.assertEqual(len(res["selected"]), 80)
        self.assertEqual({p: sum(1 for t in res["selected"] if t["pool"] == p) for p in ("client_c", "client_a")}, {"client_c": 40, "client_a": 40})
        for t in res["selected"]:
            self.assertIn(sc.bucket(self.partition_seed, t["cluster_id"]), sc.ACCEPTANCE_BUCKETS)

    def test_incident_records_only_and_no_goldens_and_one_ticket_per_cluster(self):
        res = acceptance.select_acceptance(tickets(), self.partition_seed, self.draw_seed)
        self.assertTrue(all(sc.INCIDENT_ID.fullmatch(t["ticket_id"]) for t in res["selected"]))
        self.assertFalse([t for t in res["selected"] if t["pool"] == "golden"])
        clusters = [t["cluster_id"] for t in res["selected"]]
        self.assertEqual(len(clusters), len(set(clusters)))

    def test_it_never_overlaps_a_calibration_draw_made_with_the_same_partition(self):
        cal = sc.select_calibration(tickets(), self.partition_seed, 12345, quotas={"client_c": 20, "client_a": 20})
        acc = acceptance.select_acceptance(tickets(), self.partition_seed, self.draw_seed)
        self.assertFalse({t["cluster_id"] for t in cal["selected"]} & {t["cluster_id"] for t in acc["selected"]})
        self.assertFalse({(t["pool"], t["ticket_id"]) for t in cal["selected"]} & {(t["pool"], t["ticket_id"]) for t in acc["selected"]})

    def test_no_ticket_type_exceeds_twenty_percent_of_the_hundred(self):
        res = acceptance.select_acceptance(tickets(), self.partition_seed, self.draw_seed)
        self.assertLessEqual(max(res["type_counts"].values()), 20)

    def test_the_draw_is_deterministic_and_the_seed_matters(self):
        a = acceptance.select_acceptance(tickets(), self.partition_seed, self.draw_seed)
        b = acceptance.select_acceptance(list(reversed(tickets())), self.partition_seed, self.draw_seed)
        self.assertEqual([t["ticket_id"] for t in a["selected"]], [t["ticket_id"] for t in b["selected"]])
        c = acceptance.select_acceptance(tickets(), self.partition_seed, self.draw_seed + 1)
        self.assertNotEqual([t["ticket_id"] for t in a["selected"]], [t["ticket_id"] for t in c["selected"]])

    def test_a_short_pool_is_reported_not_silently_filled(self):
        res = acceptance.select_acceptance(tickets(30), self.partition_seed, self.draw_seed)
        self.assertTrue(res["shortfall"])

    def test_the_months_are_spread(self):
        res = acceptance.select_acceptance(tickets(), self.partition_seed, self.draw_seed)
        months = {sc.month_of(t) for t in res["selected"]}
        self.assertGreaterEqual(len(months), 6)


class PrivateDrawTests(Records):
    def test_the_draw_writes_private_files_and_returns_counts_and_hashes_only(self):
        out = self.dir / "private"
        summary = acceptance.draw_private(tickets(), self.partition, self.draw, out)
        self.assertEqual((summary["cases"], summary["per_pool"]), (80, {"client_a": 40, "client_c": 40}))
        self.assertEqual(len(summary["selection_sha256"]), 64)
        self.assertTrue(summary["largest_type"] <= 20)
        text = json.dumps(summary)
        for t in tickets()[:5]:
            self.assertNotIn(t["text"], text)
        self.assertTrue((out / "acceptance_selection.json").is_file() and (out / "acceptance_cases.json").is_file())
        acceptance.verify_selection(out / "acceptance_selection.json", out / "acceptance_cases.json")
        selection = json.loads((out / "acceptance_selection.json").read_text(encoding="utf-8"))
        self.assertNotIn("text", json.dumps(selection))                                  # the committed-able file holds ids and hashes, never ticket text

    def test_it_is_drawn_once(self):
        out = self.dir / "private"
        acceptance.draw_private(tickets(), self.partition, self.draw, out)
        with self.assertRaisesRegex(acceptance.AcceptanceError, "drawn once"):
            acceptance.draw_private(tickets(), self.partition, self.draw, out)

    def test_a_shortfall_writes_nothing(self):
        out = self.dir / "private"
        with self.assertRaisesRegex(acceptance.AcceptanceError, "could not fill"):
            acceptance.draw_private(tickets(30), self.partition, self.draw, out)
        self.assertFalse(out.exists() and any(out.iterdir()))

    def test_a_changed_ticket_is_caught_by_verification(self):
        out = self.dir / "private"
        acceptance.draw_private(tickets(), self.partition, self.draw, out)
        cases = json.loads((out / "acceptance_cases.json").read_text(encoding="utf-8"))
        cases[0]["ticket_text"] += " (edited)"
        (out / "acceptance_cases.json").write_text(json.dumps(cases), encoding="utf-8")
        with self.assertRaisesRegex(acceptance.AcceptanceError, "changed since it was drawn"):
            acceptance.verify_selection(out / "acceptance_selection.json", out / "acceptance_cases.json")

    def test_bad_seed_records_are_refused(self):
        bad = self.dir / "bad.json"
        bad.write_text(json.dumps({**json.loads(self.draw.read_text(encoding="utf-8")), "seed": 7}), encoding="utf-8")
        with self.assertRaisesRegex(acceptance.AcceptanceError, "seed records are invalid"):
            acceptance.draw_private(tickets(), self.partition, bad, self.dir / "p2")
        with self.assertRaises(acceptance.AcceptanceError):
            acceptance.draw_private(tickets(), self.draw, self.draw, self.dir / "p3")           # the wrong purpose in the partition slot

    def test_the_default_output_folder_is_the_git_ignored_private_one(self):
        import subprocess
        root = pathlib.Path(__file__).resolve().parent.parent
        self.assertEqual(acceptance.PRIVATE_DIR, root / "data" / "acceptance_private")
        probe = subprocess.run(["git", "check-ignore", "-q", "data/acceptance_private/acceptance_selection.json"], cwd=root)
        self.assertEqual(probe.returncode, 0, "the private acceptance folder must be git-ignored")

    def test_the_purpose_is_a_known_seed_purpose(self):
        self.assertTrue(seed_mod.valid_purpose("acceptance-selection"))
        self.assertEqual(seed_mod.derive_seed(ARTIFACT, "acceptance-selection"), self.draw_seed)


if __name__ == "__main__":
    unittest.main()
