# PYPL (client_c) ticket load - 2026-10-14

Source: `C:\Projects\AI\SLM\data\rag\clients\client_c` (four files provided by PYPL; read, not modified).

## Before
The files had been loaded earlier only as 376 coarse chunks (`App_mapping.xlsx` 207; the three incident files 169, with the
305-ticket `Incident_l3_upt_support.txt` grouped into 137). PYPL had 0 rows in `client_ticket_records`.

## What changed
- `sync_client_rag_to_postgres.py` now recognises ServiceNow-style incident exports (CSV, or CSV saved as `.txt`; columns Number /
  Summary / Created / Assignment Group) and loads them as typed ticket records plus one incident chunk per ticket, with the
  auto-assign boilerplate removed. Before, the CSVs were filed as assets and the `.txt` as a single document.
  Tests: `tests/test_ticket_export_ingestion.py`.
- Live load (only the three incident files; `App_mapping.xlsx` was already loaded and is unchanged):
  337 tickets (L1 SAP Basis Unix Support 11, L3 SAP Basis 21, L3 UPT support 305; 2025-05 to 2026-05) in `client_ticket_records`,
  337 incident chunks in `client_knowledge_chunks`, all embedded; 3 source-file rows and 1 ingestion run recorded.
- The 169 earlier coarse chunks for the same files were retired reversibly (`knowledge_scope = 'superseded'`, so the tenant layer
  skips them) to avoid duplicate matches. To restore them: `UPDATE client_knowledge_chunks SET knowledge_scope = 'client_private'
  WHERE tenant_id = 'client_c' AND knowledge_scope = 'superseded'`.
- PFG (4,029 tickets) and the shared base were not touched.
- Backup taken first: `C:/Projects/AI/backups/2026-10-14_pre-pypl-ticket-load/` (outside the repo).

## Result
Searchable PYPL knowledge is now 207 app-mapping chunks + 337 ticket chunks. Individual tickets are retrieved: the exact
"Host not available: sapao5dbdu05" ticket scores 0.975 and similar hosts 0.63; "Bad CMS response time" finds the CMS ticket
(0.63); an out-of-domain VPN query returns only weak matches (0.34).

## Left as is
- The tenant manifest in `C:\Projects\AI\SLM\data\clients\client_c\governance` still says "ESET / PayPal" (old tree, not edited).
- Knowledge-base articles, architecture docs and the CMDB export from PYPL are still pending (per the manifest).
- Tickets carry no classification yet; the platform classifies at analysis time.
