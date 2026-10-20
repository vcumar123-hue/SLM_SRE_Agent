# R2-3 acceptance evaluation: protocol and status

Authority: design sections 13 (evaluation protocol), 17 (owner decisions), 21 (owner amendment: `rules_only`) and 22.3 (governance corrections). This file restates the order in which things happen and what
is frozen; it does not change the gates (13.6) or the rubric (13.4, 13.5).

## The one rule that governs the order

**Everything in 13.1 is fixed, committed and hashed before anyone sees or labels any acceptance ticket.** Build, then freeze, then reveal, then label, then run. Not build, label, freeze.
Until the freeze record exists the acceptance tickets (the 80 real ones **and** the 20 synthetic ones) stay private: they are not committed, not in a shared folder and not shown to Vijay or Sara.
(`data/acceptance_private/` is git-ignored for this reason; only hashes are committed at the freeze.)

## Roles (recorded in `docs/evaluation/roles.json`, enforced by `planner_experiment/roles.py`)

A labeler may hold no other role, the two labelers are different people, and look-alike names must be declared as the same person or different people (never guessed). The adjudicator, custodian and golden approver may be one person or several (an overlap among those three is recorded as a warning):

| Role | Holder | State |
|---|---|---|
| Labeler 1, labeler 2 | Vijay, Sara | named; label independently, blind, on all 100; must not see any acceptance ticket before the freeze |
| Adjudicator | Ramanathan | stated by the owner 2026-10-18 |
| Acceptance-package custodian | Ramanathan | holds the private package; the automated draw runs on a controlled machine and the developer session does not read the output |
| Golden approver (A-1) | Ramanathan | approves every golden ticket; also adjudicator and custodian under approved A-1 |
| Owner / approver of Release 2 | Vijay Ramanathan | signed the R2-2 approval |

**Current state.** A-1 was explicitly approved by Vijay Ramanathan on 2026-10-19. Ramanathan is the adjudicator, custodian and golden approver; this permitted overlap is recorded as a warning. Vijay and Sara are labelers only. Earlier inferred approvals remain withdrawn in the audit history.

## Sequence

| # | Step | Status |
|---|---|---|
| 0 | Owner states the Release 2 approval (`release.confirm_approval`); freeze reports zero problems | **done 2026-10-18**: stated in the owner's own words and signed Vijay Ramanathan; recorded verbatim; the earlier inferred approval stays in the history as withdrawn |
| 1 | Release configuration frozen (`rules_only`, runtime, tools, budgets, integration, hooks) | done (`docs/baselines/release2/rules_only_release_freeze.json`); re-confirmed after step 0 |
| 2 | Draft 20 acceptance-only synthetic goldens (`golden-acc-1..20`), disjoint from the 18 calibration goldens | **drafted privately, unapproved** (`data/acceptance_private/acceptance_golden_DRAFT.json`; separation from the calibration goldens verified, 20 distinct hosts and patterns) |
| 3 | Content approval of those goldens | **open: the owner chose to skip the two-approver rule; the replacement is the proposed amendment A-1 below, which the owner must approve in its exact wording. Until then the goldens stay private and unapproved** |
| 4 | Calibration work finishes (grounding constants stay at weight 0; blinding-check layout; agreement subset) | tooling built and tested. Stage 1 produces ticket-only ground-truth sheets with no system-selected evidence; only after both sheets are complete may stage 2 run the live arms and create blinded outputs. Both Vijay and Sara must independently score <=60% on the blinding check |
| 5 | Privately draw the 80 real tickets (40 PYPL, 40 PFG): acceptance buckets 40-79, incident-only, one per cluster, type cap 20%, seed fixed beforehand | **tool built and tested** (`planner_experiment/acceptance.py`, `cli draw-acceptance`); seed recorded (`docs/baselines/release2/seed_acceptance.json`, artifact = the tool's commit `e363d99`); **not run**: the draw is automated (owner decision) but needs a named human custodian who is not a labelling reviewer, and it runs on the controlled machine, not in the developer session |
| 6 | Build the leave-cluster-out capture for the 100 (held-out tickets and clusters removed from what the tools return) | reuses `planner_experiment.capture` / `exclusion`; acceptance wiring to build |
| 7 | Build and commit the analysis code (gate evaluation, paired bootstrap), the batch runner, the blinded review layout and the freeze script | built and tested on synthetic fixtures: gate analysis, batch runner, blinded layout, freeze, run-once, live executor and staged calibration tooling. The executor decides engagement from the fallback-free state, prevents baseline-fallback side effects from influencing the candidate, excludes held-out clusters and hashes corpus row contents before/after |
| 8 | **Freeze and hash** (13.9): gates and rubric (this protocol and design 13.4-13.6), acceptance id list and draw script, analysis code, batch runner and configuration, versions (`rules_only` release freeze) | after 2-7 |
| 9 | Reveal the blinded package to Vijay and Sara; independent labels (classification and alternatives, root cause, determinable / not determinable / indeterminate), recorded **before** any system output is shown | after 8 |
| 10 | Ramanthan adjudicates disagreements | after 9 |
| 11 | Run once: batch mode, production budgets, `rules_only`, held-out removed; infrastructure re-run rule as 13.5 | after 10 |
| 12 | Blinded semantic review of baseline vs candidate (random order, identical layout), then unblind and evaluate G1-G8 | after 11 |
| 13 | Record the result in `docs/baselines/release2/r2_3_acceptance_result.json` (gates, acceptance-set hash, decider); `on` becomes possible only if all eight gates passed, and only for a tenant with its own recorded decision | after 12 |

## What the freeze (step 8) contains

| Frozen item | Path | State |
|---|---|---|
| Gates, rubric, scoring and re-run rules | design 13.4-13.6 and this file | exists |
| Acceptance id list and the seeded draw script | `docs/evaluation/acceptance_ids.json`, `planner_experiment/acceptance.py`, `docs/baselines/release2/seed_acceptance.json` | script and seed built; ids exist after the draw |
| Acceptance golden file (hash only is committed) | `data/acceptance_private/acceptance_golden_tickets.json` | after step 3 |
| Analysis code | `planner_experiment/evaluate_release2.py` (the design's `tools/evaluate_release2.py`, kept with the other evaluation code) | built |
| Batch runner and configuration | `planner_experiment/batch_runner.py`, `planner_experiment/acceptance_run.py`, `config/agent_runtime.json` | built |
| Versions (rule table, tools, budgets, runtime) | `docs/baselines/release2/rules_only_release_freeze.json` | exists |
| Blinded review layout | `planner_experiment/review_layout.py` | built |

## Design notes for the remaining tooling (to be built and tested on synthetic fixtures before the freeze)

* **Batch runner.** Sequential, `batch` mode, production budgets, the frozen `rules_only` planner, tenant context per ticket. Every held-out acceptance ticket and its cluster must be removed from what the tools **and** the deterministic
  pipeline can retrieve (design 13.2): the exclusion matcher (`planner_experiment/exclusion.py`) applied at the retrieval boundary, failing closed. Infrastructure-only re-runs (at most two, logged; 13.5). Output records are hashed and kept private.
* **Arms.** Baseline = the deterministic pipeline with its existing fallback. Candidate = the as-deployed answer with the loop on: where the run is supported (merged or conflict) the investigation replaces the fallback and
  `reconcile` is applied to the fallback-free deterministic response; everywhere else the baseline answer stands. Because `on` is blocked until R2-3 has passed, the harness composes the candidate answer from the same
  `reconcile` and decision code the hook uses, and a parity test must show the harness and the hook agree on synthetic cases.
* **Blinded layout.** Per ticket two outputs (cause list, evidence excerpts, escalation flag) in one identical anonymised format, random order per ticket, no system ids, run ids, step traces, timings or format cues; judgements recorded before unblinding.
* **Freeze script.** Hashes the items in the table above, the reviewer layout and the acceptance package, writes the freeze record (13.9) and refuses any later change to a frozen item.

## Calibration before the freeze (13.2, 13.4)

The design requires calibration to finish before the freeze: a 60-ticket calibration set with one reviewer, a 30-ticket subset labelled by two reviewers to reach kappa >= 0.6, and a blinding check on a 20-ticket
sample. Calibration is staged: first, Vijay and Sara return ground-truth sheets containing no system output; second, the custodian generates the blinded output packets. Vijay and Sara submit independent guesses and each must score
at most 60%. Only calibration tickets are involved. A passing signed calibration result is a required frozen input.

## Methodology amendment A-1 (APPROVED 2026-10-19): content approval of the 20 acceptance goldens

Why. Design 13.2 had the two labelling reviewers approve the goldens; that would show Vijay and Sara part of the acceptance set before the freeze, which 13.1 forbids. The owner chose to skip the
"two other approvers" correction, so this approved amendment records the replacement method.

* **A-1 (recommended).** The 20 acceptance goldens are drafted by the developer from a written brief (varied infrastructure, application, identity, data and network incidents; no real client text) and checked by the
  tooling (`golden.check_separation`, ticket-type cap, 20 distinct hosts and patterns, no real ticket ids or client names). Then:
  1. They are approved by **one named content approver** who is not a labelling reviewer and not the author of the goldens (the developer). The approver may also be the adjudicator and the package custodian
     (the relaxed arrangement of 2026-10-18, recorded as a warning). The reduction from two approvers to one is a recorded deviation.
  2. **Approval is recorded per ticket** (`approved_by` and `approved_on` on each of the 20), not once for the batch.
  3. Each ticket carries its **expected causes (1 to 3) and the evidence a correct answer needs**, and the approver reviews those, not just the ticket text, before the freeze. (`golden.parse_acceptance_golden` refuses a ticket without them.)
  4. Results are reported **separately for the real tickets and for the goldens**, and a **sensitivity result excluding all 20 goldens** is included next to the gating result. Neither is gating; they exist so that a pass cannot
     rest on the synthetic tickets without anyone noticing. (`evaluate_release2` computes both.)
  5. Vijay and Sara cannot see any golden before the complete package is frozen; the goldens are not committed, and only their hash is recorded at the freeze.
* A-2. Replace the 20 synthetic tickets with 20 further real tickets (50 PYPL, 50 PFG): no content approval is needed, but the 80/20 composition of design 13.2 changes and the acceptance set is then all client data.
* A-3. Keep the two-reviewer approval of the goldens but have **different** people label the acceptance set: not viable with the reviewers named.

## Next required actions

The role gate now passes. Before the private acceptance draw:

1. Commit the completed calibration tooling and derive the calibration seed from that immutable commit.
2. Generate the ticket-only calibration label sheets for Vijay and Sara.
3. Vijay and Sara label independently and return their completed sheets.
4. Generate the blinded system-output packets only after both label sheets validate.
5. Vijay and Sara submit independent blinding guesses; both must score at most 60%, and the agreement gate must pass.
6. Ramanathan reviews and approves each of the 20 synthetic acceptance goldens.
7. Only after those checks pass may the custodian run the automated private 80-ticket draw and freeze the complete 100-ticket package.
