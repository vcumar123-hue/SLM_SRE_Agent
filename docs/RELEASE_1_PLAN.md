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
| 0 | Version control; characterization/regression tests | In progress |
| 1 | Server-side execution authorization (W1), authentication and roles (W2), external-LLM governance (W5) | Planned |
| 2 | Safety Gate evidence semantics, provenance separation, execution-time re-check (W3) | Planned |
| 3 | Composed autonomy decision: recurrence cap, conservative watch policy (W4) | Planned |
| 4 | Shared legacy/agentic finalization (W6): characterization tests first; pure normalization separated from side effects (candidate creation, trace writes) | Planned |
| 5 | Start Qdrant/PostgreSQL, run full baseline, record versioned results (W7) | Planned |

## Change manifest
Environment and dependency changes are recorded here as they happen.

| Date | Change | Reason |
|---|---|---|
| 2026-10-11 | Branch `release-1-hardening` created from `main` @ `1bc02f2` | Decision 9 |

## Two-tree rule (owner request, 2026-10-11)
All Release 1 changes are made on this branch **and** applied to `C:\Projects\AI\SLM` (the working folder with `.env`, runtime data and venv).
After each wave: copy the code/config files changed on the branch into `SLM`, but only if `SLM`'s current copy is identical to `main`'s version (no local divergence is overwritten). Never copy `.env`, `data/` runtime files or `API_Key/`. Each sync is listed in the manifest.
