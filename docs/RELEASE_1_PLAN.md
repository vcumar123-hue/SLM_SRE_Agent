# Release 1 — SLM Hardening and Agentic Parity

**Branch:** `release-1-hardening` (from `main` @ `1bc02f2`) in `C:\Projects\AI\SLM_Repo`
**Out of scope:** everything in Release 2 (agent runtime, tool registry, RCA loop, adaptive retrieval, playbook repair loop, dynamic orchestration). Do not start it on this branch.

## Exit criterion
Direct API calls cannot bypass approval, the Safety Gate, autonomy policy, role checks or tenant egress policy.

## Approved decisions (owner review, 2026-10-11)
| # | Decision |
|---|---|
| 1 | Route protection and per-principal roles are in scope. API-key → principal/role mapping for Release 1 (OIDC/JWT later). The caller-supplied `X-User-Role` header is no longer trusted. |
| 2 | Queued plans may be **simulated**, never run **live**. Live requires `approved`. |
| 3 | Re-run the Safety Gate immediately before live execution. Store each run with `gate_run_id`, queue id, plan hash, target and timestamp. |
| 4 | External model calls: **deny when policy is absent**; explicit tenant permission and provider allowlist required. |
| 5 | Evidence states: `unknown` (missing/malformed), `synthetic` (dry-mode), `estimated` (inferred rollback/error tolerance), `verified` (authoritative). Simulation may proceed on `unknown`; live and autonomous may not. Gate-generated evidence is stored separately from client evidence. |
| 6 | Recurrence is a **separate tier cap** (moderate → at most `auto_execute_watch`; high → `hold_for_approval`). In Release 1 watch mode requires human approval. |
| 7 | UI behaviour is preserved through server-generated authorization. Direct scripts/tests are updated; no bypass-compatible behaviour is kept. |
| 8 | Qdrant and PostgreSQL are started for the final baseline; the driver install and service changes are recorded in the manifest below. |
| 9 | Version control: use this repository (the `SLM` working folder is not a git checkout). |

## Autonomous execution is not inferred from the absence of approval
Autonomous mode is determined by **all** of:
1. a trusted **service identity** (principal `kind == "service"`),
2. an **autonomous registry mapping** that is enabled for the classification, and
3. a **server-created execution intent** (`intent.kind == "autonomous"`) on the queue entry.

The request's `autonomous_requested` flag carries no authority.

## Waves
| Wave | Scope | Status |
|---|---|---|
| 0 | Version control; characterization/regression tests | Done (W1/W5 regression suites; legacy `/analyze` characterization is the first task of Wave 4, before any extraction) |
| 1 | Server-side execution authorization (W1), authentication and roles (W2), external-LLM governance (W5) | Implemented (`947cd44`, `14e34c2`); verification closed by Wave 1b |
| 1b | Closure of the Wave 1 review: OS-level queue lock, durable fail-closed egress audit, stable warning overrides, UI contract and simulation-only workflow tests, clean-checkout validation | **See Wave 1b section below** |
| 2 | Safety Gate evidence semantics, provenance separation (W3). The execution-time re-check is already done in Wave 1 | On hold until Wave 1b exit criteria are met |
| 3 | Composed autonomy decision: recurrence cap, conservative watch policy (W4) | Planned |
| 4 | Shared legacy/agentic finalization (W6): characterization tests first; pure normalization separated from side effects (candidate creation, trace writes) | Planned |
| 5 | Start Qdrant/PostgreSQL, run full baseline, record versioned results (W7) | Planned |

## Change manifest
Environment and dependency changes are recorded here as they happen.

| Date | Change | Reason |
|---|---|---|
| 2026-10-11 | Branch `release-1-hardening` created from `main` @ `1bc02f2` | Decision 9 |
| 2026-10-11 | Wave 1 applied to `C:\Projects\AI\SLM` (19 files copied; each matched `main` before overwrite): `security.py`, `execution_authorization.py`, `egress_policy.py`, `automation_engine.py`, `app.py`, `ui.py`, models, agents, page modules, tests, `.env.vastai.example`, tenant template. `.env`, `data/` and `API_Key/` untouched | Two-tree rule |
| 2026-10-11 | No new Python dependencies in Wave 1. `psycopg2` is still absent from `.venv`; install is deferred to Wave 5 | Decision 8 |
| 2026-10-11 | Wave 1b: no new dependencies. `portalocker==3.2.0` was already pinned in `requirements.txt` and installed in `.venv`. `requirements.txt` unchanged | Queue lock |
| 2026-10-11 | Environment fact recorded: the project `.venv` has no Streamlit (declared in `requirements.txt`, but installing it would conflict with the pinned `pandas==3.0.2`). The system Python 3.14 has Streamlit 1.55.0 and was used for the UI-layer tests. No package was installed or changed in either interpreter | Wave 1b UI tests |
| 2026-10-11 | Wave 1b applied to `C:\Projects\AI\SLM` (26 files, each matching a known-good version before overwrite; `.gitignore` skipped; `.env`, `data/`, `API_Key/` untouched) | Two-tree rule |
| 2026-10-11 | `tests/__init__.py` disables debug logging for the test process so tests never append to `data/debug/*.jsonl` | Test hermeticity |

## Two-tree rule (owner request, 2026-10-11)
All Release 1 changes are made on this branch **and** applied to `C:\Projects\AI\SLM` (the working folder with `.env`, runtime data and venv).
After each wave: copy the code/config files changed on the branch into `SLM`, but only if `SLM`'s current copy is identical to `main`'s version (no local divergence is overwritten). Never copy `.env`, `data/` runtime files or `API_Key/`. Each sync is listed in the manifest.

## Wave 1 operational notes
- **Auth:** with no `AIOPS_PRINCIPALS`/`AIOPS_API_KEY` and `AIOPS_ENV` unset the API stays open (dev) as principal `dev-open` (human, l3). Approvals and executions are recorded under that id, not the name the UI sends. Set `AIOPS_ENV=production` plus principals for real identities; set `AIOPS_UI_API_KEY` for the UI.
- **Existing queue entries:** entries approved before this change have no approval record or plan hash, so they cannot run live. Re-run the Safety Gate and approve again.
- **External LLM:** no existing tenant has `external_llm_policy`, so Groq fallback and LLM playbook generation are denied until a tenant opts in (see `tenant_config/tenant_config_template.json`). Decisions are logged to `data/egress_audit.jsonl`.
- **Execute API:** denied requests now return HTTP 403 with the authorization decision; the UI surfaces the reason.

## Wave 1b - closure of the Wave 1 review
Scope agreed 2026-10-11 after review of Wave 1. Wave 2 stays on hold until every exit criterion below is met.

| Exit criterion | Where it is enforced / tested |
|---|---|
| External LLM decisions persist an audit or deny the call | `egress_policy.py`; `tests/test_egress_audit.py` |
| Lock contention cannot stall indefinitely and returns a clear 503 | `automation_engine._queue_lock` (portalocker, bounded); `tests/test_queue_lock.py` |
| Cross-process double-execution tests pass | `tests/test_queue_lock.py::DoubleExecutionAcrossProcessesTests` |
| Warning approval records reason and stable warning identities | `execution_authorization.warning_identities`, `approve_automation`; `tests/test_override_workflow.py` |
| Execution rejects new or changed warnings | `authorize_execution` + `compare_warnings`; `tests/test_override_workflow.py` |
| Autonomous execution cannot override warnings | `authorize_execution`; `tests/test_override_workflow.py`, `tests/test_execution_authorization.py` |
| Updated UI sends valid queue-based payloads | `ui.py` (demo steps, helpers), `page_modules/automation_queue.py`; `tests/test_ui_workflow.py` |
| Simulation-only UI workflow passes | `tests/test_ui_workflow.py` (API layer authoritative; UI helper and AppTest layers) |
| All tests pass in `SLM_Repo`, `SLM` and a clean checkout | see "Validation record" below |

### Design decisions recorded
- **Lock:** OS-level lock (`portalocker`, already pinned in `requirements.txt`) on a persistent file that is never deleted; released automatically when the owner dies; default wait 10 s (`AIOPS_QUEUE_LOCK_TIMEOUT`); a timeout raises `QueueBusyError` and the API returns HTTP 503 with `Retry-After`, naming the operation and queue id. When queue state moves to PostgreSQL, replace this with row/advisory locking.
- **Audit:** PostgreSQL preferred, JSONL fallback (`fsync`). For an external endpoint the decision is persisted before the call is permitted and the call is denied if no sink persisted it. `AIOPS_ALLOW_UNAUDITED_EXTERNAL_LLM` (default false) is ignored when `AIOPS_ENV=production`. Local endpoints are never blocked; their usage is recorded best-effort. The PostgreSQL sink has not been exercised against a live database (driver absent until Wave 5); when it is enabled, confirm the connection timeout so an unreachable database cannot delay external calls.
- **Warning overrides:** identity is `check_id` + `warning_code` + `evidence_fingerprint` (stable condition fields only, never text or timestamps). Approval of a warning gate requires an explicit override and reason and stores approver, gate run id, plan hash, target and the acknowledged warning identities. Execution re-runs the gate with the *approved* parameters, rejects new warnings, materially changed evidence, a changed target or plan, still requires an execute-time reconfirmation, and never allows an autonomous override.
- **UI:** the demo's Safety Gate and Execute steps now create a real queue entry, bind a gate run to it and execute in **simulation only**. The queue page asks for an override reason when the gate reports warnings. Server refusals (401/403/409/503) are shown with their reason. `smoke_test.py` sends a bearer token when `AIOPS_SMOKE_API_KEY`, `AIOPS_UI_API_KEY` or `AIOPS_API_KEY` is set.

### How to run the tests
The project `.venv` has no Streamlit (installing it would conflict with the pinned `pandas`), so the suite is run with two interpreters:

    .venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py" -t .
        API layers run; the Streamlit UI layers are skipped (11 tests)
    C:\Users\vcuma\AppData\Local\Programs\Python\Python314\python.exe -m unittest discover -s tests -p "test_*.py" -t .
        this interpreter has Streamlit: nothing is skipped

The failures reported from the reviewer's sandbox (egress audit rows missing, authorization suite stalling) could not be reproduced. Failure injection showed the previous designs were fragile in exactly that class of environment, and both are corrected: every wait is bounded and every failure is logged.

### Validation record (code commit `3db464c`, 2026-10-11)
118 tests: 62 from Wave 1 plus 56 added in Wave 1b (lock 9, durable audit 15, override workflow 18, UI/API workflow 14).

| Tree | `.venv` interpreter | System Python 3.14 (Streamlit) |
|---|---|---|
| `SLM_Repo` | 118 run, OK, 11 skipped (UI layers) | 118 run, OK, 0 skipped |
| `SLM` | 118 run, OK, 11 skipped (UI layers) | 118 run, OK, 0 skipped |
| Clean clone of the branch (no `.env`, no untracked files) | 118 run, OK, 11 skipped (UI layers) | 118 run, OK, 0 skipped |

- `SLM` holds the same content as the branch for all 27 changed files (compared line-ending-insensitively).
- The `.venv` run also passes with `ResourceWarning` treated as an error.
- The suite does not modify `SLM`'s runtime debug logs (file timestamps identical before and after).

### Not verified
- A real browser session of the Streamlit UI. The UI layers use the real `ui.py` helpers and Streamlit `AppTest` against the real FastAPI app, with the Safety Gate, evidence, connectors and storage paths replaced; no live connector can run.
- The queue page's override-reason input is covered through the helpers and the server contract, not by driving that page in `AppTest` (it needs candidate and registry state).
- The PostgreSQL audit sink (`insert_egress_audit`) against a live database; the driver is absent until Wave 5.
- The reviewer's original sandbox. The failures reported there were not reproduced; see the note above.
