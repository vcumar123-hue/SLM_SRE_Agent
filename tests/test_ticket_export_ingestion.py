"""
ServiceNow-style incident exports (CSV, or CSV saved as .txt) must load as ticket records plus one searchable chunk per ticket.
They used to be filed as assets (CSV) or as one giant document (.txt).

Run from the project root:  python -m unittest tests.test_ticket_export_ingestion -v
"""
import csv
import importlib.util
import pathlib
import tempfile
import unittest

try:
    import sync_client_rag_to_postgres as sync
except Exception:  # psycopg2 / pandas missing in this interpreter
    sync = None

HEADER = ["Number", "Opened by", "Summary", "Created", "Resolved", "Assignment Group", "Assigned To", "Active",
          "Details", "Findings", "Actions taken", "Incident state", "Work notes"]
NOISE = ("2026-04-05 13:00:06 - System Administrator (Work notes)\nThis Incident was not assigned to anyone in the last 24 hours. "
         "Hence system has auto-assigned this either to the active group manager or an active assignment group member.\n")


def write_export(path, rows):
    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(HEADER)
        writer.writerows(rows)


ROWS = [
    ["INC1000001", "A. Person", "Host not available: sapdb01", "2026-04-04 00:36:06", "2026-04-07 07:20:59", "L1 SAP BASIS Unix Support",
     "B. Person", "FALSE", "The host sapdb01 does not respond to a network ping and was rebooted.", "Kernel panic after patching",
     "Restarted the instance and verified services", "Closed", NOISE],
    ["INC1000002", "SAP Solution Manager", "Bad CMS response time on the reporting server", "2026-05-01 10:00:00", "", "L3 SAP Basis",
     "C. Person", "TRUE", "Response time above threshold for 20 minutes.", "", "", "In Progress", ""],
]


@unittest.skipIf(sync is None or getattr(sync, "pd", None) is None, "sync script dependencies are not installed here")
class TicketExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())

    def run_file(self, name):
        return sync.process_file(self.tmp / name, "client_x", "client_private", "client_x_index", None)

    def test_csv_export_becomes_ticket_records_and_incident_chunks(self):
        write_export(self.tmp / "incidents.csv", ROWS)
        result = self.run_file("incidents.csv")
        self.assertEqual(result["source_category"], "ticket_dump")
        tickets = result["source_rows"]["client_ticket_records"]
        chunks = result["source_rows"]["client_knowledge_chunks"]
        self.assertEqual(len(tickets), 2)
        self.assertEqual(len(chunks), 2)
        self.assertEqual(result["source_rows"]["client_asset_records"], [])
        self.assertTrue(all(c["knowledge_type"] == "incident" for c in chunks))

    def test_ticket_fields_are_mapped(self):
        write_export(self.tmp / "incidents.csv", ROWS)
        first = self.run_file("incidents.csv")["source_rows"]["client_ticket_records"][0]
        self.assertEqual(first["ticket_id"], "INC1000001")
        self.assertEqual(first["caller"], "A. Person")
        self.assertEqual(first["assignment_group"], "L1 SAP BASIS Unix Support")
        self.assertEqual(first["opened_at"], "2026-04-04 00:36:06")
        self.assertEqual(first["resolved_at"], "2026-04-07 07:20:59")
        self.assertEqual(first["short_description"], "Host not available: sapdb01")
        self.assertEqual(first["tenant_id"], "client_x")

    def test_an_open_ticket_has_no_resolved_time(self):
        write_export(self.tmp / "incidents.csv", ROWS)
        second = self.run_file("incidents.csv")["source_rows"]["client_ticket_records"][1]
        self.assertIn(second["resolved_at"], (None, ""))

    def test_chunk_text_has_the_story_without_system_boilerplate(self):
        write_export(self.tmp / "incidents.csv", ROWS)
        content = self.run_file("incidents.csv")["source_rows"]["client_knowledge_chunks"][0]["content"]
        self.assertIn("Host not available: sapdb01", content)
        self.assertIn("Kernel panic after patching", content)
        self.assertIn("Restarted the instance", content)
        self.assertNotIn("auto-assigned", content)
        self.assertNotIn("System Administrator", content)

    def test_a_txt_file_holding_a_ticket_export_is_read_as_one(self):
        write_export(self.tmp / "tickets.txt", ROWS)
        result = self.run_file("tickets.txt")
        self.assertEqual(result["source_category"], "ticket_dump")
        self.assertEqual(len(result["source_rows"]["client_ticket_records"]), 2)

    def test_an_ordinary_text_document_is_still_a_document(self):
        (self.tmp / "notes.txt").write_text("This is a plain document about the escalation process, written in sentences, "
                                            "not a table, with enough words to be meaningful content.", encoding="utf-8")
        self.assertEqual(self.run_file("notes.txt")["source_category"], "document")

    def test_a_non_ticket_csv_keeps_its_previous_handling(self):
        with open(self.tmp / "assets.csv", "w", encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(["Hostname", "IP address", "Environment", "Owner"])
            writer.writerow(["sapdb01", "10.0.0.5", "production", "Basis team"])
        result = self.run_file("assets.csv")
        self.assertNotEqual(result["source_category"], "ticket_dump")
        self.assertEqual(result["source_rows"]["client_ticket_records"], [])

    def test_ids_are_stable_so_a_rerun_updates_instead_of_duplicating(self):
        write_export(self.tmp / "incidents.csv", ROWS)
        a = self.run_file("incidents.csv")["source_rows"]
        b = self.run_file("incidents.csv")["source_rows"]
        self.assertEqual([r["record_id"] for r in a["client_ticket_records"]], [r["record_id"] for r in b["client_ticket_records"]])
        self.assertEqual([c["chunk_id"] for c in a["client_knowledge_chunks"]], [c["chunk_id"] for c in b["client_knowledge_chunks"]])

    def test_column_detection(self):
        self.assertTrue(sync.looks_like_ticket_columns(HEADER))
        self.assertTrue(sync.looks_like_ticket_columns(["Number", "Short description", "Opened", "Assignment group"]))
        self.assertFalse(sync.looks_like_ticket_columns(["Hostname", "IP address"]))
        self.assertFalse(sync.looks_like_ticket_columns(["Number", "Summary"]))


if __name__ == "__main__":
    unittest.main()
