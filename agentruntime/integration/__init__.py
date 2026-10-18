"""
R2-2 application integration (design sections 3, 8, 10 and 21).

Wraps the frozen Release 2 core (`agentruntime.release`) without changing it. Nothing here runs unless the loop's mode for the request's tenant resolves to
`shadow` or `on`; with the default (`off`), `hook.after_analysis` returns the very object it was given, and nothing beyond `gate` is imported on the request path.

  gate.py       mode resolution: environment, per-tenant recorded deployment decision, R2-3 result for `on`, release-freeze check, kill switch (rollback)
  engagement.py when the loop is worth running (the engagement rule of design 3.2)
  shadow.py     the bounded, non-blocking, idle-aware shadow queue (design 8)
  reconcile.py  merging a supported investigation with the deterministic result, or recording the conflict (design 10.2)
  hook.py       the one entry point the application calls after a response is built
"""
