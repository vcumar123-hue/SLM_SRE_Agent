# Planner experiment: owner runbook

Status at commit `c8dbb82` plus the commands added after it (see the git log): the harness is reviewed and approved (design section 20). Nothing has been
drawn, labelled, frozen or run. R2-2 stays blocked until the experiment selects a planner. Commands run from the repository root.

## Who may hold what (custody)

There are two kinds of machine and person, and the difference is the whole point of the blinding.

* **Key holder** (the owner, or a gated CI run by the owner): holds the experiment key, and is the only party that runs the commands that need it:
  `draw-experiment`, `build-cases`, `create-freeze`, `run-experiment`. The case package (`build-cases` output) contains the text of the 50 drawn tickets.
* **Developer** (including an AI assistant): never holds the key, never runs those four commands, and never sees the drawn tickets, the case package,
  the recording, the annotations or the results before the freeze. A developer may run only `select-development` and `freeze-planners` (neither needs
  the key) and builds the planners on the 10-ticket development subset. Recording the reviewers and the partition seed are the owner's.
* **Reviewers** receive the case package from the key holder and return their annotation files. They never receive the key.

The key is 64 hex characters in `data/planner_experiment.key` (git-ignored) or the environment variable `AIOPS_PE_KEY`. Do not set that variable in a
shared shell, a developer's profile, a shared CI environment or a container image. A machine without the key fails closed, by design.

| # | Step | Who | Command / action |
|---|---|---|---|
| 1 | Create the experiment key | **Key holder** | `python -m planner_experiment.cli init-key` |
| 2 | Back it up | **Key holder** | copy the key to a password manager or offline store; never to the repository, chat, e-mail, a developer, or a reviewer |
| 3 | Record the two reviewers | **Owner** | `python -m planner_experiment.cli reviewers --names "First Last" "First Last" --hours 50 50 --recorded-by "Owner Name"` (at least 40 hours each; 50 preferred; written once) |
| 4 | Prepare, review and approve calibration golden tickets | Reviewers, then owner | see "Golden tickets" below; at least **12**, a few extra advised |
| 5 | Draw the development subset (4 PYPL, 4 PFG, 2 golden) | Developer (no key needed) | `python -m planner_experiment.cli select-development --artifact <full 40-character commit id, fixed beforehand>` |
| 6 | Build and tune the three planners **on that subset only** | Developer | `agentruntime/planners/` (`rules_only`, `rules_then_model`, `model`); never look at the 50-ticket set |
| 7 | Freeze the planners and runtime | Developer (no key needed) | `python -m planner_experiment.cli freeze-planners --round 1 --planner rules_only=agentruntime.planners.<module>:<Class> --planner rules_then_model=... --planner model=...` |
| 8 | Draw the 50-ticket planner-selection set | **Key holder** | `python -m planner_experiment.cli draw-experiment --round 1` (seed derived from the planner-freeze hash; needs the key) |
| 9 | Build the blinded case package | **Key holder** | `python -m planner_experiment.cli build-cases --round 1 --out <file outside the repository>`; hand it to the reviewers, not to developers |
| 10 | Independent labelling | Both reviewers, separately | annotation sheets (`planner_experiment.annotations.template_for`), one file per reviewer, every case |
| 11 | Adjudicate every disagreement, with a written rationale in `notes` | Owner | end-state kappa must be at least 0.6 |
| 12 | Capture the scripted tool recording for the 50 cases | **Key holder** | **no command yet** (library: `planner_experiment.scripted_env.capture_case`); to be added and tested before this step, and it must run on the key holder's side because it processes the drawn tickets |
| 13 | Create the experiment freeze | **Key holder** | `python -m planner_experiment.cli create-freeze --round 1 --cases <file> --recording <file> --annotations <adjudicated file> --reviewer-file "First Last"=<file> --reviewer-file "First Last"=<file>` (the configuration and injection cases are generated from the workflow; a round after the first includes the earlier rounds' selections automatically) |
| 14 | Run once | **Key holder** | `python -m planner_experiment.cli run-experiment --record <freeze record>`; one attempt per freeze, a failed run keeps its claim |
| 15 | If a planner passes the frozen rule, promote it unchanged into R2-2 | Developer | loop remains `off` by default; no tenant `shadow`/`on` without a separate recorded decision |
| 16 | Container and PostgreSQL suites, then the 100-ticket acceptance evaluation and R2-3 | Developer / reviewers | before the release gate; the acceptance set's goldens are a separate, acceptance-only set (design 13.2) |

## Golden tickets (step 4)

A draft of 18 synthetic tickets is in `docs/drafts/golden_tickets_DRAFT.json` with **no approvals** (`approved_by` is empty). It is a proposal, not an
approved file: `data/golden_tickets.json` does not exist and the draw tooling will not read the draft.

1. Each reviewer reads every ticket independently and decides whether to approve it. Reject or rewrite anything that is unrealistic, contains real client
   data, copies a real ticket, duplicates another draft's opening words, or could not be run against the `poc_client` knowledge. Edit the draft file freely
   before approving (it is only a draft).
2. When **both** reviewers have approved a set of tickets, record exactly those ids:

   `python -m planner_experiment.cli approve-golden --draft docs/drafts/golden_tickets_DRAFT.json --ids golden-1 golden-2 ... --approved-by "First Last" "First Last"`

   This writes `data/golden_tickets.json` once, with `approved_by` set to the two recorded reviewers, only for the ids you list (there is no "approve all").
   It records an approval; it cannot make one. Names are written only after the review has happened.
3. Format of an item (and what the tooling checks): `ticket_id` is `golden-<n>`, unique, never `golden-acc-*`; `set` is `calibration`; non-empty `text`,
   `host` and `opened_at`; `approved_by` is exactly the two reviewers; `tenant_id` names the tenant whose knowledge it runs against (default `poc_client`).
4. Keep the opening words and the hosts varied: no ticket type may exceed 20% of a set (2 of 10 in the development subset, 10 of 50 in the experiment).
5. Calibration goldens are calibration-only. The acceptance set's own 20 goldens are prepared separately (`data/acceptance_golden_tickets.json`, ids
   `golden-acc-*`), disjoint from these, and **must not exist while planners are being developed**.

## Eligibility and voiding the development draw

* Only real tickets whose id is `INC` followed by digits can be drawn. Changes, catalog tasks and problem records are excluded in every draw and re-checked
  at the freeze (design 20.11).
* The development subset is drawn once. If a defect is found **before any planner is frozen**, `python -m planner_experiment.cli void-development
  --reason "..." --authorised-by "..."` keeps the old draw (renamed), logs the void and permits a redraw with the same seed record. It is refused once
  planners are frozen. Tell the reviewers whenever it is used.

## Key custody correction (2026-10-15)

The key first created on the development machine is **retired** (check value `1160a0b798c63a8b`): treat it as seen by the development session. Draws, case
building, freezes and runs refuse it. To put a proper key in custody:

1. On the **separate key-holder machine** (the owner's, or gated CI), with a checkout of this repository: `python -m planner_experiment.cli init-key`, back the key up
   outside the repository, then `python -m planner_experiment.cli key-check`. It prints only a 16-character check value. **Do not copy the key to the
   development machine or paste it anywhere.**
2. On the development machine: `python -m planner_experiment.cli register-custody --check <value> --custodian "Person, machine"`; commit `key_custody.json`
   (check value only).
3. The key-holder machine also needs access to the ticket database and the approved golden file: it runs `draw-experiment`, `build-cases`, `create-freeze`
   and `run-experiment`. Delete the retired key file on the development machine when convenient.

## Voids, acknowledgements and the model manifest

* Every void of the development draw must be acknowledged by **both** reviewers before `freeze-planners` will run: send them
  `docs/REDRAW_NOTICE_FOR_REVIEWERS.md`, then record each reply with `python -m planner_experiment.cli acknowledge-void --voids 1 2 --by "Reviewer Name" --note "..."`.
* A selection is valid only under the algorithm version that made it (`selection_version`, currently 3). If selection logic changes, a test fails until the
  version is advanced; older draws must then be voided and drawn again.
* `python -m planner_experiment.cli verify-model` checks the model manifest (`agentruntime/planners/model_manifest.py`) against the running LM Studio and the
  model file. A production `freeze-planners` needs the manifest complete (prompt versions filled in).
* The development selection files and `data/planner_dev/` stay out of git (they contain text from real client tickets).

## What not to do

* Do not copy the key to a developer or reviewer machine, paste it into chat, or put it in the repository. With the key, the recorded draw can be
  dictionary-attacked; without it, it cannot (design 20.6).
* Do not pass a seed by hand: none of the commands accepts one.
* Do not tune planners after step 7, and do not look at the 50 tickets or any output on them before freezing. A second round needs revised
  planners (developed on the development subset) and a fresh set, drawn under the same key.
* Do not try to rehearse the frozen run: there is no dry-run route by design (design 20.4).
* Do not add anyone's name to `approved_by` by hand to make the tooling accept a file; the approval has to be real.
