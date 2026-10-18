# R2-3 acceptance evaluation: protocol and status

Authority: design sections 13 (evaluation protocol), 17 (owner decisions), 21 (owner amendment: `rules_only`) and 22.3 (governance corrections). This file restates the order in which things happen and what
is frozen; it does not change the gates (13.6) or the rubric (13.4, 13.5).

## The one rule that governs the order

**Everything in 13.1 is fixed, committed and hashed before anyone sees or labels any acceptance ticket.** Build, then freeze, then reveal, then label, then run. Not build, label, freeze.
Until the freeze record exists the acceptance tickets (the 80 real ones **and** the 20 synthetic ones) stay private: they are not committed, not in a shared folder and not shown to Vijay or Sara.
(`data/acceptance_private/` is git-ignored for this reason; only hashes are committed at the freeze.)

## Roles

| Role | Who | Notes |
|---|---|---|
| Labelling reviewers | Vijay, Sara | label independently, blind, on all 100; must not see any acceptance ticket (real or synthetic) before the freeze |
| Adjudicator | Ramanthan | resolves disagreements |
| Content approvers of the 20 acceptance goldens | **two named people who are neither Vijay nor Sara** | *to be named by the owner*; `golden.parse_acceptance_golden` refuses approvers who are the reviewers |
| Draw and package custodian | the owner (or a gated machine they control) | runs the seeded draw; the developer does not read the acceptance tickets |

## Sequence

| # | Step | Status |
|---|---|---|
| 0 | Owner states the Release 2 approval (`release.confirm_approval`); freeze reports zero problems | **open: the earlier approval was inferred and has been withdrawn** |
| 1 | Release configuration frozen (`rules_only`, runtime, tools, budgets, integration, hooks) | done (`docs/baselines/release2/rules_only_release_freeze.json`); re-confirmed after step 0 |
| 2 | Draft 20 acceptance-only synthetic goldens (`golden-acc-1..20`), disjoint from the 18 calibration goldens | **drafted privately, unapproved** (`data/acceptance_private/acceptance_golden_DRAFT.json`; separation from the calibration goldens verified, 20 distinct hosts and patterns) |
| 3 | Two content approvers (not Vijay or Sara) approve those goldens | **open: approvers to be named** |
| 4 | Calibration work finishes (grounding constants stay at weight 0; blinding-check layout; agreement subset) | open: needs reviewers; see "Calibration before the freeze" |
| 5 | Privately draw the 80 real tickets (40 PYPL, 40 PFG): acceptance buckets 40-79, incident-only, one per cluster, type cap 20%, clusters of the development subset excluded, seed fixed beforehand | tooling to build; run by the owner's custodian |
| 6 | Build the leave-cluster-out capture for the 100 (held-out tickets and clusters removed from what the tools return) | reuses `planner_experiment.capture` / `exclusion`; acceptance wiring to build |
| 7 | Build and commit the analysis code (gate evaluation, paired bootstrap), the batch runner, the blinded review layout and the freeze script | to build |
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
| Acceptance id list and the seeded draw script | `docs/evaluation/acceptance_ids.json`, `tools/draw_acceptance_set.py` | to build |
| Acceptance golden file (hash only is committed) | `data/acceptance_private/acceptance_golden_tickets.json` | after step 3 |
| Analysis code | `tools/evaluate_release2.py` | to build |
| Batch runner and configuration | `tools/batch_runner.py`, `config/agent_runtime.json` | to build |
| Versions (rule table, tools, budgets, runtime) | `docs/baselines/release2/rules_only_release_freeze.json` | exists |
| Blinded review layout | `tools/review_layout.py` | to build |

## Calibration before the freeze (13.2, 13.4)

The design requires calibration to finish before the freeze: a 60-ticket calibration set with one reviewer, a 30-ticket subset labelled by two reviewers to reach kappa >= 0.6, and a blinding check on a 20-ticket
sample (reviewers' guess of which output is which must be at most 60%). Under the owner amendment the planner no longer needs tuning, but these checks still gate the rubric and the review layout. They need Vijay and Sara
**on calibration tickets only**, which is permitted before the freeze. Open: whether the owner wants them done as designed or amends them in writing.

## Needed from the owner now

1. The Release 2 approval statement in the owner's own words (step 0).
2. The names of the two people who approve the content of the 20 acceptance goldens (step 3), or a written methodology amendment instead.
3. Who runs the private draw and holds the acceptance package (step 5).
4. Whether to run the calibration checks as designed (step 4) or amend them.
