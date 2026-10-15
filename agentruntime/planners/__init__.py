"""
Planner implementations for the Release 2 loop (rules_only, rules_then_model, model).

Every module in this package is hashed into the planner-experiment freeze record, and the experiment refuses to run a planner whose code
does not live here. So the planners that are compared are exactly the files in this folder, and the planner selected by the experiment is
promoted to R2-2 **unchanged**: nothing is copied or rewritten between the experiment and the product. A planner may import only from
`agentruntime` (itself hashed) and the standard library, so the hash covers everything it depends on.

No planner is implemented yet: writing them, and tuning the rule table on the calibration set, happens before the freeze.
"""
