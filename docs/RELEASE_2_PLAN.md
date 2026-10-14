# Release 2 — Bounded Investigation Agent (plan for review)

**Status:** proposal for owner review. Nothing in this document is built yet.
**Branch:** continues on `agentic-v2` (Release 1 is tagged `release-1`).
**Builds on:** `docs/AGENTIC_DESIGN.md` (sections 5–9 and 10, phases A2 and B). That document stays the detailed reference; this plan
re-states the scope against what Release 1 actually delivered and what we measured since.

## 1. Goal and the rule that does not change
Give the platform one **bounded, read-only investigation loop** for the cases where the one-shot analysis is unsure, and prove with
measurements that it helps before it is allowed to run by default.

**Authority never moves to the agent.** The agent can read, hypothesise and cite evidence. It cannot execute, approve, change
policy, publish knowledge or grant autonomy; those stay with the deterministic gates built in Release 1.

## 2. Where we start (verified)
Release 1 delivered what the design called phases A0 and A1:
- Server-side execution authorization, roles, approval bound to the plan hash, Safety Gate evidence semantics, composed
  autonomy decision with a recurrence cap, tenant egress policy (deny by default, audited).
- One shared post-RCA finalization for the legacy and agentic analysis paths, with side-by-side parity tests and a stored baseline.

Changes since the design was written that Release 2 should use:
- Retrieval is now **PostgreSQL + pgvector** (tenant, learned and base layers; recall 0.97–0.99; 0.2–0.6 s per search).
  The design's `incident_kb_search` tool wraps `retrieval_engine.retrieve_incidents` unchanged.
- PYPL now has **337 individual ticket records** with resolution history in `client_ticket_records` (PFG has 4,029). That makes
  a `ticket_history_lookup` tool and a real evaluation set possible.
- The outcome ledger matches **strictly by tenant**, so an outcome tool cannot leak another client's track record.
- Existing building blocks: `agents/a2a_protocol.py` (in-process bus, `TraceSpan`), `agents/orchestrator.py` (fixed pipeline behind
  `AIOPS_AGENTIC_ORCHESTRATOR_ENABLED`, default off), an `agent_traces` table (summaries only, no tool calls, model or token fields).

Not verified yet (the first task measures them): how reliably a 4B local model (Gemma 3 4B via LM Studio) picks a tool when the
output is constrained to a JSON schema, and how long one loop step takes on this machine.

## 3. Scope

| Phase | Deliverable | Exit criterion |
|---|---|---|
| **R2-0 Foundations** | `AgentPolicy` / `AgentRunResult` contracts; tool registry with input validation; runner with step, tool-call and wall-clock budgets; size-capped, sanitised tool results; record/replay of tool results and model outputs; trace schema extended (tool calls, model, tokens) | Contracts reviewed; unit tests for budgets, denial, timeout and replay; traces persisted |
| **R2-1 Read-only tools** | `incident_kb_search`, `ticket_history_lookup` (tenant tickets by host, service, time window), `outcome_lookup`, `pattern_lookup`, `evidence_lookup` | Each tool tenant-scoped, schema-validated, covered by tests that another tenant's data never appears |
| **R2-2 Investigation loop, shadow mode** | Loop inside the RCA step behind `AIOPS_AGENTIC_RCA_LOOP_ENABLED` (default off). In shadow mode it runs beside the deterministic path, is logged and scored, and **does not change the answer** | Shadow runs on the evaluation set complete within budget; zero policy violations |
| **R2-3 Evaluation and promotion** | Evaluation set, metrics, thresholds; decision to move L0 → L1 (engages only when uncertain) | Meets the promotion criteria below versus the stored baseline |
| **R2-4 Progress in the UI** | Synchronous with timeout first; progress events and an investigation view (steps, evidence, uncertainty, escalation) | Operator can see why the loop concluded or escalated |

**Engage only when (any):** confidence below `CONFIDENCE_THRESHOLD`, retrieval quality below `MIN_RETRIEVAL_SCORE`, conflicting
evidence, no reusable knowledge, or high impact with an uncertain cause. **Limits:** 4–6 tool calls, 45–60 s, read-only tools,
at most one query reformulation per source, every material hypothesis cites at least one evidence reference, and the loop escalates
when evidence is insufficient or contradictory. **Confidence stays owned by the central confidence engine**, which gains an optional
`evidence_grounding` input; the agent never self-reports confidence.

## 4. Evaluation
- **Set:** the Release 1 golden tickets plus a held-out slice of PYPL tickets whose closure notes record the real cause and fix
  (for example host-not-available and response-time tickets). The owner confirms the ground truth for the slice.
- **Metrics:** correct-classification rate; evidence-grounded RCA rate; unsupported-cause rate; escalation appropriateness;
  unnecessary external LLM calls; median and p95 latency.
- **Promotion rule:** move to L1 only if the loop improves the evidence-grounded rate, does not raise the unsupported-cause rate, stays
  within the latency budget and causes zero egress-policy violations. Numeric thresholds are set after the shadow results exist.

## 5. Decisions needed from you (with my recommendation)
1. **Planner model.** Recommend the local Gemma model with schema-constrained output, shadow mode first. An external model only for a
   tenant whose egress policy allows it (none do today).
2. **Hosting.** Recommend synchronous with a 45–60 s timeout in Release 2; an asynchronous job later if the latency data demands it.
3. **Shadow first.** Recommend yes: no change to what operators see until R2-3 passes.
4. **Evaluation ground truth.** Which PYPL and PFG tickets may be used, and who confirms the real cause and fix?
5. **Tools without a source.** CMDB and change lookups need data PYPL has not sent (CMDB export, change records). Recommend leaving
   them out of Release 2 and adding them when the data arrives.
6. **Forecasting.** The TimesFM idea (a `metric_forecast` tool for trend and anomaly context) is not in Release 2. Telemetry history is
   thin (382 events), so there is nothing to forecast on yet. Recommend revisiting after the shadow results.
7. **Merge timing.** When `agentic-v2` should be merged into `main` (Release 1 is already tagged).

## 6. Risks
| Risk | Mitigation |
|---|---|
| A 4B model picks tools unreliably | Measure first (task 1 below); constrained schema; bounded steps; deterministic fallback on any failure |
| Latency in a synchronous API | Hard wall-clock budget; shadow mode hides it from users during evaluation |
| Prompt injection from ticket, KB or CMDB text | Tool results are untrusted data: size-capped, sanitised, never executed, read-only tools only |
| Non-deterministic runs undermine comparison | Pinned sampling settings; record/replay of tool results and model outputs |
| Data from one client reaching another | Every tool takes the tenant from the server, never from the model; tests per tool |
| Client data sent externally by a multi-step loop | Egress policy checked before each external model call; audited |

## 7. Not in Release 2
Adaptive retrieval, playbook generate–validate–repair, knowledge-gap proposals, dynamic orchestration, any agent execution
authority, forecasting. They reuse the Release 2 runtime and are planned after its results.

## 8. First concrete step (before any runtime code)
A small experiment, about a day of work: take 50 tickets (golden set plus PYPL), ask the local model to choose among the five read-only
tools using a JSON-schema-constrained response, and record how often it picks a sensible tool, produces valid output and stays
inside the time budget. The result decides whether the local planner is viable or whether decision 1 needs another answer.
