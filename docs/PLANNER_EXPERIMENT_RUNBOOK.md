# Planner experiment: owner runbook

Status at commit `c8dbb82`: the harness is reviewed and approved (design section 20). Nothing has been drawn, labelled, frozen or run. R2-2 stays blocked
until the experiment selects a planner. This page is the order of work and who does each step. Commands run from the repository root on the **controlled
draw/run machine** (yours, or a gated CI), never on a developer's or reviewer's machine.

| # | Step | Who | Command / action |
|---|---|---|---|
| 1 | Create the experiment key | **Owner**, controlled machine | `python -m planner_experiment.cli init-key` |
| 2 | Back it up | **Owner** | copy `data/planner_experiment.key` (64 hex characters) to a password manager or offline store; it is git-ignored and must never be committed, e-mailed, or given to developers or reviewers |
| 3 | Record the two reviewers | **Owner** | `python -m planner_experiment.cli reviewers --names "First Last" "First Last" --hours 50 50 --recorded-by "Owner Name"` (at least 40 hours each; 50 preferred; written once) |
| 4 | Prepare calibration golden tickets | Reviewers / owner | see "Golden tickets" below; at least **12**, a few extra advised |
| 5 | Draw the development subset (4 PYPL, 4 PFG, 2 golden) | Developer, on the controlled machine | `python -m planner_experiment.cli select-development --artifact <full 40-character commit id fixed beforehand>` |
| 6 | Build and tune the three planners **on that subset only** | Developer | `agentruntime/planners/` (`rules_only`, `rules_then_model`, `model`); never look at the 50-ticket set |
| 7 | Freeze the planners and runtime | Developer | `python -m planner_experiment.cli freeze-planners --round 1 --planner rules_only=agentruntime.planners.<module>:<Class> --planner rules_then_model=... --planner model=...` |
| 8 | Draw the 50-ticket planner-selection set | Controlled machine | `python -m planner_experiment.cli draw-experiment --round 1` (seed derived from the planner-freeze hash; needs the key) |
| 9 | Build the blinded case package | Controlled machine | `python -m planner_experiment.cli build-cases --round 1 --out <file outside the repository>` |
| 10 | Independent labelling | Both reviewers, separately | annotation sheets (`planner_experiment.annotations.template_for`), one file per reviewer, every case |
| 11 | Adjudicate every disagreement, with a written rationale in `notes` | Owner | end-state kappa must be at least 0.6 |
| 12 | Capture recordings and injection cases; create the experiment freeze | Controlled machine | `planner_experiment.freeze.create_freeze` (library call: no command yet; add `prior_selection:<n>` files for rounds after the first) |
| 13 | Run once | Controlled machine | `python -m planner_experiment.cli run-experiment --record <freeze record>`; one attempt per freeze, a failed run keeps its claim |
| 14 | If a planner passes the frozen rule, promote it unchanged into R2-2 | Developer | loop remains `off` by default; no tenant `shadow`/`on` without a separate recorded decision |
| 15 | Container and PostgreSQL suites, then the 100-ticket acceptance evaluation and R2-3 | Developer / reviewers | before the release gate; acceptance goldens are a separate, acceptance-only set (design 13.2) |

## Golden tickets (step 4)

A JSON list in `data/golden_tickets.json` (the only file the development tooling reads), each item:

```json
{
  "ticket_id": "golden-1",
  "text": "SAP login failing for all users after password policy change",
  "host": "sapprd01",
  "opened_at": "2026-05-01 10:00:00",
  "set": "calibration",
  "approved_by": ["First Last", "First Last"],
  "tenant_id": "poc_client"
}
```

* `ticket_id` starts with `golden-` and is unique; **never** `golden-acc-` (reserved for the acceptance set).
* `set` is `calibration`. `approved_by` must be **exactly** the two reviewers in the reviewer record (case-insensitive), and nobody else.
* `tenant_id` names the tenant whose knowledge the ticket is run against (default `poc_client`).
* Vary the wording and the first two words: no ticket type may exceed 20% of a set (2 of 10 in the development subset, 10 of 50 in the experiment), so
  twelve tickets with the same opening words will be rejected. Give each a distinct host.
* Calibration goldens are calibration-only. The acceptance set's own 20 goldens are prepared separately (`data/acceptance_golden_tickets.json`,
  ids `golden-acc-*`), disjoint from these, and **must not exist while planners are being developed**.

## What not to do

* Do not copy the key to a developer or reviewer machine, paste it into chat, or put it in the repository. With the key, the recorded draw can be
  dictionary-attacked; without it, it cannot (design 20.6).
* Do not pass a seed by hand: none of the commands accepts one.
* Do not tune planners after step 7, and do not look at the 50 tickets or any output on them before freezing. A second round needs revised
  planners (developed on the development subset) and a fresh set, drawn under the same key.
* Do not try to rehearse the frozen run: there is no dry-run route by design (design 20.4).
