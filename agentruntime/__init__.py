"""
Bounded investigation runtime (Release 2, phase R2-0).

A small, read-only runtime for the RCA investigation loop. The design is in docs/RELEASE_2_DESIGN.md; the rules that matter here:

  * the tenant comes from a TrustedContext built by the server, never from a model;
  * every run has hard budgets that configuration can only lower;
  * tools are read-only and return evidence objects that a deterministic verifier checks;
  * stored run data is sanitised, tenant-keyed, encrypted by default and expires;
  * any failure returns a result object, never an exception, and the deterministic analysis stays the fallback.

The mode is `off` unless AIOPS_AGENTIC_RCA_LOOP says otherwise and the configuration is valid (see config.resolve_mode).
Nothing in this package imports execution, approval or configuration-writing code, and a test enforces that.
"""

__all__ = ["SCHEMA_VERSION"]

SCHEMA_VERSION = "1"
