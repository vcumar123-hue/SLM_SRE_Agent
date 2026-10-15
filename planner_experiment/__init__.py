"""
Planner experiment harness (design section 12).

Everything needed to run the pre-registered trajectory experiment, and nothing that decides it:
  * select_cases   - seeded, cluster-aware CALIBRATION draw (acceptance tickets are never read here)
  * scripted_env   - tool outputs captured once, then replayed identically for every planner mode
  * injection      - the planted-instruction cases and their deterministic success test
  * annotations    - the annotation schema, validation and reviewer agreement
  * metrics        - trajectory scoring, bootstrap intervals and the fixed decision rule
  * experiment     - runs every mode over every case through the real agentruntime.Runner
  * freeze         - hashes the experiment inputs; refuses to run unless the freeze record matches

The planners being compared (rules_only, rules_then_model, model) are supplied by the caller; this package defines the interface they are
tested through and ships only trivial planners for its own self-tests.
"""
HARNESS_VERSION = "pe-0.1"
