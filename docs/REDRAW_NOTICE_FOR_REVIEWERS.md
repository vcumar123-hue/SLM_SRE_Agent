# Notice to the reviewers: redraw of the development subset

To: Vijay, Sara. Date: 2026-10-15. Please reply to confirm you have received and read this; your acknowledgement is recorded beside the void records
and the planners cannot be frozen until both of you have acknowledged.

## What happened

The development subset is the 10 tickets (4 PYPL, 4 PFG, 2 golden) on which the three candidate planners are built and tuned. It is drawn once. It has been
drawn **three times** in total; the first two draws were voided and are kept, and the third stands.

| # | What was wrong | Authorised by | Outcome |
|---|---|---|---|
| Void 1 | The first draw included four PFG records that were Change requests (CHG), not incidents: the draw had no rule for what counts as an incident. PFG's 4,029 records are 2,029 incidents, 1,000 changes, 855 service-catalog tasks and 145 problem records | the project owner, in the working session on 2026-10-15 | An incident-only rule was added (a real ticket's id must be exactly INC followed by digits) and the subset was redrawn |
| Void 2 | The redrawn file was produced by the new rule but still carried the old algorithm version label (2). The version is now 3 and is enforced everywhere a draw is used | the project owner, instruction "review and fix as per the feedback", 2026-10-15 | Redrawn so the label is true. The selection came out **identical** to the redraw it replaced (same seeds, same algorithm) |

## What did not change

* The golden tickets you approved are unchanged (golden-1 to golden-18 remain approved). Two of them, `golden-2` and `golden-4`, are in the development subset.
* The experiment design, the decision rule, the planner-selection set (50) and the acceptance set (100) are unchanged, except that the same incident-only
  rule now applies to them, and is checked again when a set is frozen.
* No planner, recording, annotation or freeze existed when either void happened, and none depended on the voided draws.
* The development tickets are developer-visible by design. Nothing you will label (the 50 and the 100) has been drawn, seen or run.

## What we ask

Please reply "acknowledged" (or raise any concern). Reply separately; each acknowledgement is recorded under your own name. If you think a void should not
have been allowed, say so now: the planners have not been built or frozen, and the process can still be changed.

Details: design document sections 20.11 and 20.12; the void records are in `docs/baselines/planner_experiment/development_voids.json`.
