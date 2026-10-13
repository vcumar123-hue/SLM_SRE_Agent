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
| 2 | Safety Gate evidence semantics, provenance separation (W3). The execution-time re-check is already done in Wave 1 | Implemented - see the Wave 2 section below |
| 3 | Composed autonomy decision: recurrence cap, conservative watch policy (W4) | Implemented - see the Wave 3 section below |
| 4 | Shared legacy/agentic finalization (W6): characterization tests first; pure normalization separated from side effects (candidate creation, trace writes). Also the model-file cleanup below | Implemented on `agentic-v2` - see the Wave 4 section below |
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
| 2026-10-12 | Wave 2: no new dependencies; no packages installed or changed. New module `safety_evidence.py`; gate-generated evidence now lives in `data/evidence/_generated/` (git-ignored) | Wave 2 |
| 2026-10-13 | Wave 3: no new dependencies. Added centralized autonomy policy and a composed, fail-closed autonomy decision used by enablement, eligibility and execution authorization | Wave 3 |

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

## Wave 2 - Safety Gate evidence semantics and provenance (W3)

### What changed
- Every piece of evidence is judged on two independent axes. **What it says** (raw status `pass | warn | fail | unknown`; anything missing, malformed or unrecognised is `unknown`, never `pass`) and **how far it can be trusted** (provenance `verified | synthetic | estimated | unknown`).
- Provenance is decided by the platform, not by the file: `verified` = pulled from a configured API, pushed by an authenticated principal (`submitted_by` recorded), or captured from real state (CMDB / lab container). `synthetic` = dry-mode snapshot or dry-run simulation. `estimated` = inferred by the platform (blast radius, rollback readiness). `unknown` = origin cannot be established, including uploaded or manual files. A `provenance` field written inside a client file is ignored.
- Effective status by execution class (`evidence_policy` in `safety_gate_config.json`; defaults shown):

| Condition (would otherwise pass) | simulation | live | autonomous |
|---|---|---|---|
| Missing / unreadable / malformed / unrecognised status / unattested / synthetic / estimated | `unknown` | `warn` (override required) | `fail` (blocked) |
| Stale beyond TTL (default 72 h) | `warn` | `warn` | `fail` |

  A reported `warn` or `fail` is never softened. `degraded_status` can only be configured to `unknown`, `warn` or `fail`, never `pass`. Simulation proceeds (the gate reports `warning`; it does not block simulation); live needs a recorded human override; autonomous is blocked.
- **Separate folders.** The gate writes only to `<evidence_dir>/_generated/` (drift, error-budget/simulation, rollback readiness). It never writes over client evidence. Where both exist, a source that actually reports a problem wins; otherwise the most trustworthy source wins, so degraded generated evidence cannot veto verified client evidence.
- **Honest labels.** The "error budget" check is the dry-run simulation result and says so. "Rollback readiness" is an estimate and no longer claims "Rollback artifacts available".
- **Gate modes.** The gate evaluates `simulation`, `live` and `autonomous` classes; autonomous queue entries are gated as autonomous (the stricter staleness/evidence rules never applied before). `warning` identities (Wave 1b) are mode-independent so an approval granted from a simulation-class run still matches the live re-run, and a change in the underlying condition (for example a snapshot failure) is rejected.
- **Gate report.** New fields: `execution_mode_class`, `evidence_policy`, `evidence_provenance` (counts), `unverified_checks`, and per check `raw_status`, `provenance`, `evidence_issue`, `evidence_source`, `policy_reasons`, `evidence_sources_considered`. `enforcement_mode` is now `server_enforced`.
- **Defects fixed on the way.**
  1. The shipped config's `pilot_defaults.missing_evidence_status: "pass"` made missing evidence pass, and the `policy` block in the same file was never read by any code. Both are replaced by `evidence_policy`.
  2. Evidence files that start with a byte-order mark (`concurrent_activity.json`, `rollback_readiness.json`) failed to parse and were silently treated as missing, then passed by default. They are now read correctly.
  3. `POST /evidence/submit` returned HTTP 500 for every request: the root `models.py` copy of `EvidenceSubmitRequest` lacked `details`, `checked_at` and `confidence` (present in `agents/models.py`). This was already the case on `main`. The two model files have drifted; see the follow-up below.
  4. The gate overwrote client evidence files of the same names on every run.
  5. `POST /evidence/submit` and `POST /evidence/run_api_pull` now require an authenticated principal with role l2 or above, and push evidence records who submitted it.

### Operational notes
- **Default behaviour changes.** With the shipped configuration and no integrated evidence, every gate run reports `warning`: nothing is verified. Approval therefore needs an override reason (the queue page prompts for it), live execution needs execute-time reconfirmation, and autonomous execution is blocked until evidence is verified. To make a client's checks `verified`: push evidence with an authenticated principal (`POST /evidence/submit`), configure an API pull, or - only if the client accepts file uploads as authoritative - set `evidence_policy.treat_uploaded_files_as_verified: true`.
- **Client push scripts** now need a bearer token for a principal with role l2 or above.
- **Evidence files are not tamper-proof.** Provenance is an assertion recorded when evidence is written; anyone with write access to the evidence folder can forge it. Signed evidence or database-backed evidence is the long-term answer.
- **Disabled checks** remain `pass` with provenance `not_applicable` (an explicit client decision). Consider refusing autonomous execution when any check is disabled (Wave 3).
- **Existing `data/evidence/*.json` in `SLM`** that the old gate had overwritten are recognised by their `source_system` and labelled `synthetic` / `estimated` instead of being trusted.

### Follow-ups noticed, not done
- **Model files (decided 2026-10-12: do in Wave 4).** `models.py` (root) is authoritative for the HTTP request models: `app.py` and `agents/analyze.py`, `automation.py`, `evidence.py` and `telemetry.py` import it, and it carries the newer fields (`tenant_id`, `promote_to_global`, `promoted_by`, and `TelemetryStatusUpdateRequest`). `agents/models.py` is authoritative only for the agentic contracts (`TriageInput`, `RCAOutput`, `OrchestratorState` and 10 more), which exist nowhere else. It also holds a second copy of 28 request models: 24 identical, 4 drifted (`CreatePlaybookRequest`, `CreateResolutionRequest`, `PromoteCandidateRequest`, `TelemetryPromoteRequest`), and only `FallbackControlRequest` is imported from it (by `agents/knowledge.py`; identical in both). Planned cleanup: keep `agents/models.py` for the agentic contracts only, delete its duplicated request models, point `agents/knowledge.py` at `models`, and add a test that fails if a class name is defined in both files. Until then, any change to a request model must be made in the root `models.py`.
- Rollback readiness can only become `verified` through client evidence or integration; the platform does not yet verify rollback artifacts itself.

### Wave 2 validation record (code commit `fe55fb9`, 2026-10-12)
180 tests: 118 from Waves 0-1b plus 62 new in Wave 2 (`tests/test_safety_gate_evidence.py`).

| Tree | `.venv` interpreter | System Python 3.14 (Streamlit) |
|---|---|---|
| `SLM_Repo` | 180 run, OK, 11 skipped (UI layers) | 180 run, OK, 0 skipped |
| `SLM` | 180 run, OK, 11 skipped (UI layers) | 180 run, OK, 0 skipped |
| Clean clone of the branch (no `.env`, no untracked files) | 180 run, OK, 11 skipped (UI layers) | 180 run, OK, 0 skipped |

- `SLM` holds the same content as the branch for all 33 changed files (each matched a known-good version before it was overwritten).
- The suite does not modify `SLM`'s runtime data and does not create `bak/` or `data/evidence/_generated/`.
- Read-only preview against `SLM`'s real evidence (nothing written, gate not run): all six checks are stale (June timestamps against a 72 h TTL) and none is verified, so live execution reports `warn` for every check (override needed) and autonomous reports `fail`. Three files are old gate-written files now recognised as synthetic or estimated; three are manual/uploaded and therefore unattested. The two files that start with a byte-order mark now parse.

### Not verified in Wave 2
- A real gate run against `SLM`'s evidence folder, CMDB or lab containers (the tests replace the snapshot engine; a real run writes `data/evidence/_generated/`, may take snapshots and creates `./bak`).
- A real browser session of the Streamlit UI; the new provenance labels were not rendered in a browser.
- `POST /evidence/run_api_pull` against a real source system (network bound); only the marker contract is tested.

## Wave 4 - Legacy/agentic analysis parity (`agentic-v2` only)

### What changed
- Added characterization tests that pin legacy dispatch, agentic failure fallback, response shape, confidence logging and knowledge linkage before refactoring.
- Root `models.py` is the single source for HTTP request models. `agents/models.py` now contains agent contracts only, with a structural regression test preventing duplicate class definitions.
- Legacy and agentic analysis now share final confidence display, canonical domain resolution, deterministic post-RCA classification guards and final incident filtering.
- Agentic fallback now creates a governed knowledge candidate under the same conditions as legacy, feeding the existing draft and approval lifecycle.
- Successful analysis completion tracing runs from the common dispatcher, covering either path once. PostgreSQL remains best-effort and optional.
- The agentic result now includes the legacy UI/workflow response contract while retaining agent trace metadata as additive fields.

### Validation
- Characterization checkpoint: 186 tests passed, 11 Streamlit-dependent tests skipped.
- Final Wave 4 regression: 194 tests passed; 11 Streamlit-dependent tests skipped in the project `.venv`.
- Changes were made only in `C:\Projects\AI\SLM_Agentic`, per owner direction.

## Wave 3 - Composed autonomy decision and recurrence policy (W4)

### What changed
- Autonomous eligibility is now one server-side, fail-closed decision composed from registry state, outcome-ledger history, confidence, safety configuration and tenant deployment stage.
- Recurrence is an independent tier cap. Moderate recurrence can permit watch mode only; high recurrence forces human approval even when the historical success rate is high.
- Watch mode requires human approval in Release 1. A client-controlled request field cannot grant autonomous authority.
- Autonomous enablement, eligibility mapping and execution authorization use the same policy result, preventing their decisions from drifting.
- Disabled Safety Gate checks prevent autonomous execution. Missing policy or evaluation errors also fail closed.
- Tenant configuration and the template now carry an explicit deployment stage used by the decision.

### Wave 3 validation record (2026-10-13)
- Full suite: 182 tests run, all passed; 11 Streamlit-dependent tests skipped in the project `.venv`.
- The tested source/config/test/document baseline was synchronized to both `C:\Projects\AI\SLM` and `C:\Projects\AI\SLM_Agentic`; 196 included files were compared by hash with no missing or different files.
- Runtime state and secrets were excluded from synchronization: `.env`, virtual environments, `data/`, `API_Key/`, caches, logs, backups and generated evidence.
