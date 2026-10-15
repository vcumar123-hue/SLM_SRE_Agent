# Release 2 — Bounded Investigation Agent (plan, revision 2)

**Status:** revision 3 after the second owner review. **Artifacts A–D (trusted context and tool boundary, evidence, budgets and failure
states, shadow lifecycle) are approved for R2-0 foundation work and the planner experiment** (the experiment proceeds once its annotations and
decision rule are frozen). **Artifact E (pre-registered evaluation) was corrected in this revision and awaits re-review. R2-2 runtime
integration remains unapproved** until E is accepted and the owner decisions in section 5 are recorded. Nothing here is built yet.
**Branch:** continues on `agentic-v2` (Release 1 is tagged `release-1`).
**Detail:** `docs/RELEASE_2_DESIGN.md` is the engineering design; this plan keeps scope, phases and decisions. `docs/AGENTIC_DESIGN.md`
remains the long-range reference.

## 1. Goal and the rule that does not change
Give the platform one **bounded, read-only investigation loop** for the cases where the one-shot analysis is unsure, and prove with a
pre-registered evaluation that it helps before it is allowed to run by default.

**Authority never moves to the agent.** It can read, hypothesise and cite evidence. It cannot execute, approve, change policy, publish
knowledge or grant autonomy; those stay with the deterministic gates built in Release 1.

## 2. Where we start (verified)
Release 1 delivered what the design called phases A0 and A1: server-side execution authorization and roles, approval bound to the plan hash,
Safety Gate evidence semantics, a composed autonomy decision with a recurrence cap, tenant egress policy (deny by default, audited), and
one shared post-RCA finalization with parity tests and a stored baseline.

Since then:
- Retrieval is **PostgreSQL + pgvector** (tenant, learned and base layers; recall 0.97–0.99; 0.2–0.6 s per search). The retrieval function
  `retrieve_incidents` takes `tenant_id`, `knowledge_scope`, `search_order` and `collection_name`, and **defaults `tenant_id` to `client_a`**,
  so the agent's search tool is a **boundary wrapper** that fixes all of these on the server, not a pass-through.
- PYPL has **337 ticket records** and PFG 4,029. **Neither set carries usable closure notes** (PYPL: no findings or actions-taken fields,
  7 executive summaries; PFG: no resolution field), so ground truth for evaluation must be **labelled by reviewers**, not read from tickets.
- Outcome matching is strict by tenant; the outcome ledger is empty.
- Existing pieces: `agents/a2a_protocol.py`, `agents/orchestrator.py` (flagged, default off), an `agent_traces` table (summaries only).

## 3. Scope

| Phase | Deliverable | Exit criterion |
|---|---|---|
| **Now** | Accept artifacts A–E; run the **trajectory-level planner experiment** (design section 12) | artifacts accepted; experiment decision rule applied |
| **R2-0 Foundations** | `agentruntime/` contracts, `TrustedContext`, tool registry, runner with enforced budgets, sanitising, evidence store, run tables | contract, boundary and runner tests pass; a stub run persists and replays |
| **R2-1 Read-only tools** | `incident_kb_search`, `ticket_history_lookup`, `outcome_lookup`, `pattern_lookup`, `evidence_lookup` as tenant-bound wrappers returning verified evidence objects | each tool passes two-tenant isolation, cap and injection tests |
| **R2-2 Investigation loop, shadow** | loop inside the RCA step behind `AIOPS_AGENTIC_RCA_LOOP=off\|shadow\|on` (default off). Shadow runs are queued after the response and **never awaited**; they do not change the answer | batch and shadow runs finish inside budget; zero safety violations; flag `off` unchanged |
| **R2-3 Evaluation and promotion** | labelling, calibration, then **one** acceptance run against a protocol frozen **before any acceptance ticket is seen** | gates G1–G8 reported with confidence intervals under the scoring rules |
| **R2-4 Investigation view** | `investigation` block in the response and an operator panel; per-tenant `on` for one pilot tenant if R2-3 passes | operator sees steps, evidence and why it concluded or escalated |

**Flow:** deterministic initial RCA → engagement decision → optional bounded investigation → reconciliation → central confidence
recalculation → shared finalization → answer or escalation. The loop **augments** the deterministic path and replaces the one-shot Groq
fallback **only when it returns `supported` and reconciliation accepts it**; otherwise the existing path, including the Groq fallback,
runs as today. On a classification conflict the deterministic classification is **always** kept, the loop's alternative is attached, `escalate=true` is set and an operator resolves it. There is **no automatic override in Release 2**; conflicts and their resolutions are logged so a later release can decide on evidence.

**Engage when (any):** confidence below `CONFIDENCE_THRESHOLD`, retrieval quality below `MIN_RETRIEVAL_SCORE`, no reusable knowledge,
conflicting evidence, or high impact with an uncertain cause.

**Budgets (default / hard maximum, enforced in code; configuration can only lower them):** steps 8 / 10; tool calls 5 / 6; model calls
5 / 7; model-call timeout 15 s / 30 s; tool timeout 6 s / 10 s; wall clock 40 s / 45 s; tokens per run 4,000 / 6,000; result size
4,000 / 6,000 characters; evidence items 20 / 30; one query reformulation. Planning, retries and finishing use steps, not tool calls.
Expiry behaviour and the failure-state table are in design section 7.

**Confidence stays with the central engine**, which gains an optional `evidence_grounding` input computed from evidence the verifier
accepted. The agent never reports confidence.

## 4. Evaluation (pre-registered)
- **Two sets, never mixed.** Calibration (60 tickets, tunes the rule table, prompts and grounding constants, reusable) and acceptance
  (100 tickets, used **once**). Both are stratified across PYPL, PFG and golden tickets, with no ticket type above 20%, and near-duplicate
  tickets are kept in one set. Held-out tickets and their duplicates are **removed from the retrieval index** during evaluation.
- **Ground truth by reviewers.** Two independent reviewers on all acceptance tickets, owner adjudicates, Cohen's kappa at least 0.6 on a
  calibration subset before labelling continues. Labels: classification, root cause, and *determinable / not determinable / indeterminate*.
- **Freeze rule.** The gates, rubric, composition and the list of acceptance ticket ids, analysis code, runner and exact configuration are
  committed and hashed **before anyone sees or labels any acceptance ticket** (freeze record, design 13.9). Nothing changes after that.
- **Scoring.** Every eligible acceptance ticket stays in the denominators. Failures, timeouts, invalid output and unsupported completions
  count as unsuccessful for the grounding and escalation gates and cannot improve any numerator; only tickets the reviewers label
  *indeterminate* may be excluded, capped at 15% of the set. Infrastructure failures (and only those) may be re-run twice under a
  predeclared rule with the configuration unchanged. Semantic review is **blinded and randomised** (baseline and candidate in one anonymised
  layout, with a blinding check before the freeze), and labels are made before reviewers see any system output.
- **Gates (all must pass):** evidence-grounded rate +10 points with lower interval bound above +3; unsupported-cause rate within +2 points
  (interval upper bound at most +5); classification non-inferior within −3 points; schema-valid results at least 98% and failures plus
  timeouts at most 5%; p95 latency at most 45 s and median at most 25 s; escalation recall at least 70% and precision at least 80%; zero
  cross-tenant leakage, egress violations, successful injections (of at least 20 cases) and boundary violations; **record/replay integrity
  100%** for valid, version-matched records. All with 95% paired bootstrap intervals.
- **Live repeatability** (not a gate): 30 calibration tickets run 5 times each, reporting how often the tool sequence, final status and
  cause set are identical.
- **A failed attempt burns the acceptance set.** It is analysed on calibration data and a new acceptance set is drawn before retrying.
- **Also reported:** tool calls, tokens and cost, unnecessary external calls, human override rate, and recurrence after recommendations
  (not measurable until outcomes exist).

## 5. Decisions (recommendation, status)
1. **Planner model.** Rules first, the local model only for judgement if the experiment supports it; external only for a tenant whose
   egress policy allows it (only PYPL today: Groq, basic redaction, approved by the owner). *Open: confirm.*
2. **Hosting.** Synchronous with a 40 s deadline in Release 2. *Open.*
3. **Shadow.** Queued after the response, never awaited; evaluation uses a separate batch runner. *Proposed.*
4. **Ground truth and reviewers.** Reviewer labels; **who the two reviewers are, and the capacity for about 160 labelled tickets, is open.**
5. **Tools without a source.** CMDB and change lookups stay out until PYPL sends the data.
6. **Forecasting.** Out of scope; revisit after the shadow results.
7. **Merge timing.** *Owner to confirm it is intentional that `agentic-v2` merges to `main` after R2-2 and before promotion.* Recommendation: yes, because the loop is inert at its default `off`, tests assert responses identical to today at `off`, and enabling it for a tenant is a separate decision after R2-3. The alternative is to merge after R2-3.
8. **Stored run data.** Sanitised evidence objects only, tenant-keyed, 90-day default retention, never sent externally. *Open: encryption
   level (host disk encryption, optional column encryption).*
9. **Gate values** in section 4 are proposed; the owner approves or amends them before they are hashed, and before any acceptance ticket is seen.
10. **Classification conflicts:** *resolved* as above (conservative, operator resolves, no automatic override).
11. **Planner-model policy:** *owner to confirm* (local first; external planner only for a tenant whose egress policy allows it).

## 6. Risks
| Risk | Mitigation |
|---|---|
| A 4B model picks tools unreliably | rules first; trajectory experiment with a fixed decision rule; deterministic fallback on any failure |
| Latency | hard budgets; shadow never delays users; batch runner for evaluation |
| Shadow runs slow real requests on the shared local model | one worker, start only when no user analysis is running, bounded queue |
| Prompt injection from ticket, KB or note text | results are untrusted data in a delimited block; read-only tools; injection cases in the experiment and gate G7 |
| Cross-tenant data | server-held `TrustedContext`; no tenant, scope or collection argument in any tool schema; two-tenant tests; defence-in-depth check on results |
| Stored runs become a second copy of client data | sanitised immutable evidence objects, no raw output, no ticket text, retention and deletion |
| Evaluation leakage and tuning to the result | calibration/acceptance split, leave-cluster-out, gates frozen before the run, acceptance used once |
| Labelling effort or rater disagreement | kappa gate, adjudication, explicit capacity decision |

## 7. Not in Release 2
Adaptive retrieval, playbook generate–validate–repair, knowledge-gap proposals, dynamic orchestration, any agent execution authority,
forecasting, CMDB and change tools.

## 8. First concrete step (before any runtime code)
The **trajectory experiment** in design section 12: 50 calibration tickets and at least 12 injection cases, tool outputs captured once into a
scripted environment, three planner modes compared on complete trajectories (schema validity, allowed and appropriate tools,
finish or escalate correctness, repeated calls, injection resistance, latency, tokens), with the decision rule fixed in advance.
Labelling the 60 calibration tickets and the rubric agreement check run in parallel.
