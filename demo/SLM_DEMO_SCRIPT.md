# AI SRE Platform — Client Demo Script

> **Version:** July 2026 | **Runtime:** ~35 minutes | **Audience:** CIO / CISO / Head of Infrastructure

---

## Demo Overview

This demo walks through a real-time incident lifecycle on the AI SRE Platform — from first alert to closed ticket with full audit evidence. Two alerts fire simultaneously. One targets `linux-lab-02`, a standalone critical application server where auto-remediation is prohibited by policy — the platform classifies, analyses, and prepares the full remediation package, then waits for a human decision. The other targets `linux-lab-01`, a redundant web tier where policy permits autonomous action — the platform approves, validates in isolation on a drift snapshot, and executes without human intervention. By the end you see one engineer handling both incidents in under 35 minutes, with a complete governance export covering MTTR, SLA compliance, and tamper-evident audit evidence — the same artefacts your compliance team would use for a post-incident review or an auditor request.

---

## Pre-Demo Setup

Run these steps before the client enters the room. Target state: all containers running, platform healthy, incidents cleared, telemetry seeded.

```bash
# 1. Start containers
docker start linux-lab-01 linux-lab-02 network-lab-01

# 2. Start platform services (if not already running)
#    Backend:  http://localhost:8001
#    Frontend: http://localhost:8501
#    Confirm with: curl http://localhost:8001/health

# 3. Seed lab vulnerabilities and misconfigurations
bash demo/seed_vulnerabilities.sh

# 4. Seed CMDB entries (registers CI tiers and redundancy metadata)
python demo/seed_cmdb.py

# 5. Seed initial findings (creates baseline finding records)
python demo/seed_findings.py

# 6. Clear any stale incidents from prior runs
#    In Streamlit UI: Incidents → select all resolved/open → Archive
#    Or via API:  PATCH /incidents/{id}/status  {"status": "archived"}

# 7. Reset automation queue
#    Queue tab → clear completed / cancelled entries

# 8. Open browser tabs in order (full-screen each before starting):
#    Tab 1 — Streamlit UI:     http://localhost:8501
#    Tab 2 — Telemetry view:   http://localhost:8501  (navigate to Telemetry)
#    Tab 3 — Grafana:          http://localhost:3000   (overview dashboard)
#    Tab 4 — Terminal:         python demo/run_demo.py  (ready, not yet run)

# 9. Confirm safety gate evidence files present:
ls data/evidence/
# Expected: drift_status.json  error_budget_status.json  concurrent_activity.json
#           change_conflict.json  rollback_readiness.json  blast_radius.json
```

**Presenter check:** Streamlit UI loads without errors. `/health` returns `{"status": "ok"}`. Incident Console is empty or shows only archived items. Three containers show `running` in Docker Desktop.

---

## Demo Architecture

| Machine | Tier | Criticality | Redundancy | Demo Path |
|---|---|---|---|---|
| `linux-lab-02` | App Tier (standalone) | HIGH — criticality=2 | None (single instance) | **Path A — Human Approval required** |
| `linux-lab-01` | Web Tier | MEDIUM — criticality=3 | Active-active pair | **Path B — Auto-remediation permitted** |
| `network-lab-01` | Network / Infra | HIGH | None | Observer only (not touched in this demo) |

The CMDB records these attributes — tier, criticality score, redundancy type — and the error tolerance engine reads them at classification time to determine which execution path applies. The client never needs to configure this per-incident; it is derived from their existing CMDB data.

---

## The Two Incident Paths

**Path A — linux-lab-02 (CPU saturation, App Tier standalone):** Platform classifies, correlates, generates runbook, evaluates tolerance, populates the automation queue, runs the safety gate — then stops and presents an approval button. Nothing touches the live system until a human clicks Approve. After approval, a drift snapshot is captured, the fix is tested in isolation on the snapshot, verification passes, and only then does Ansible execute against the live host.

**Path B — linux-lab-01 (CPU saturation, Web Tier redundant):** Same classification and runbook path. At the tolerance evaluation step the engine reads `redundancy_type=active-active` and `criticality=3` and sets `should_auto_remediate=true`. Policy auto-approves the queue entry. The snapshot, isolation test, and Ansible execution happen automatically. The engineer watches on screen — no clicks required.

Both paths write the same audit trail. The difference is human involvement, not auditability.

---

## Step-by-Step Demo Walkthrough

---

### Step 1: Demo Environment Ready — Show the Three Containers Running

**Screen:** Terminal / Docker Desktop, or Streamlit UI → System Health panel

**Action:** Run `docker ps --filter name=linux-lab` in terminal, or open Docker Desktop and show the Containers list.

**Say:** Before we start, here is the lab environment — three Linux containers standing in for real hosts. `linux-lab-01` and `linux-lab-02` are the targets for today. The platform backend is already connected to them via SSH, and Prometheus is scraping their metrics every 15 seconds. Everything you will see is live against these hosts, not a recording.

**Expected output:** Terminal shows three rows with `STATUS: Up`. Docker Desktop shows green indicators. If using the Streamlit health panel, all three hosts show a green `RUNNING` badge.

---

### Step 2: Alert Fires — Telemetry Console Shows Incoming Metrics Breaching Thresholds

**Screen:** Streamlit UI → Telemetry tab

**Action:** Run the demo seeder in the terminal (`python demo/run_demo.py`) to trigger the alert injection, or manually navigate to Telemetry and show the live metric feed.

**Say:** We have an on-call engineer starting their shift. Two alerts land within seconds of each other — CPU saturation on `linux-lab-02` and elevated load on `linux-lab-01`. The telemetry panel is pulling from Prometheus in real time. You can see the threshold breach highlighted in amber — that is the trigger that creates the incidents we are about to work through.

**Expected output:** Telemetry panel shows time-series metrics for both hosts. CPU utilisation on `linux-lab-02` is shown above the configured threshold (e.g. >85%). The panel shows a `BREACH` label or amber indicator against the breaching metric. A notification badge appears on the Incidents tab.

---

### Step 3: Incidents Created — Show the Incident Console with the Two New Incidents

**Screen:** Streamlit UI → Incidents tab

**Action:** Click the Incidents tab. Let the page load. Point to the two new incident cards.

**Say:** The platform has automatically created two incidents — one per alert. Each card already shows the affected host, the severity the system assigned, and the current status: `New`. Nothing has been actioned yet. The engineer has not touched either of these. The platform is about to help them decide what to do with both, simultaneously.

**Expected output:** Two incident cards visible. `INC-xxx — linux-lab-02 — CPU Saturation — HIGH — New` and `INC-yyy — linux-lab-01 — CPU Saturation — MEDIUM — New`. Timestamps are within the last few minutes.

---

### Step 4: AI Classification — Click Analyse on the CPU Saturation Incident (linux-lab-02)

**Screen:** Streamlit UI → Incidents → INC-xxx (linux-lab-02)

**Action:** Click the incident card to open the detail view. Click the **Analyse** button in the AI Analysis panel.

**Say:** The engineer clicks Analyse. The platform sends the incident context — hostname, metric readings, recent change log — to the classification engine. This is not a simple keyword match. It is pulling from a RAG knowledge base built on your own resolved incidents, as well as cross-client patterns where your governance policy permits.

**Expected output:** A spinner appears for 3–8 seconds. The panel populates with a classification label (`cpu_saturation_runaway_process` or similar), a confidence score, and a short explanation of what evidence led to that classification. A confidence meter widget renders below the classification label.

---

### Step 5: Confidence Meter — Show the Classification with HIGH Confidence

**Screen:** Streamlit UI → Incident detail → AI Analysis panel → Confidence meter

**Action:** Point to the confidence meter widget. Hover over it or click to expand the breakdown.

**Say:** The confidence meter is not a single number — it breaks down into three components: evidence strength, knowledge coverage, and temporal relevance. HIGH confidence here means the platform has seen this classification pattern before, the evidence is consistent, and similar incidents resolved cleanly with a known remediation. If confidence were LOW, the system would flag that explicitly and widen the safety constraints.

**Expected output:** Confidence meter shows `HIGH` (e.g. 0.82 or above). The breakdown shows three bars — evidence strength, knowledge coverage, temporal relevance — each with a label. No warnings or low-confidence flags on this incident.

---

### Step 6: Temporal Correlation — Show Related Events with Domain Confidence Scores

**Screen:** Streamlit UI → Incident detail → Temporal Correlation panel

**Action:** Scroll down to the Temporal Correlation section. Expand it.

**Say:** The platform looks back across the event timeline — deployments, config changes, prior incidents — and scores each related event for relevance. You can see domain confidence labels: HIGH means the platform is confident this event is causally linked, MEDIUM means it is correlated but not confirmed, LOW means it is flagged for completeness. This gives the engineer a structured hypothesis, not a wall of raw log data to wade through.

**Expected output:** A list of correlated events with timestamps and confidence labels (e.g. `Deployment linux-lab-02 — 14:32 UTC — MEDIUM`, `Prior CPU saturation resolved 3 days ago — HIGH`). Each event shows a short relevance note. The list is ordered by confidence descending.

---

### Step 7: Peer Insights — Show Similar Incidents from Other Clients with Before/After Timelines (Anonymised)

**Screen:** Streamlit UI → Incident detail → Peer Insights panel

**Action:** Scroll to the Peer Insights section. Expand it.

**Say:** Where your governance policy allows, the platform surfaces anonymised patterns from similar incidents across other environments — same classification, same tier type. Each insight shows what the environment looked like before and after remediation, and how long it took. The client identity is never exposed. You can set your own policy on whether to contribute to this pool — for this client, external contribution is disabled, but they still receive insights from the global pattern base.

**Expected output:** Two or three peer insight cards, each showing: incident type, environment tier, before/after metric delta, time-to-resolve. Client identifiers are replaced with `[Client-redacted]`. A governance note is visible: `Inbound: enabled | Outbound contribution: disabled (client_a policy)`.

---

### Step 8: AI Runbook Generated — Show the Validated Checks and Remediation Actions

**Screen:** Streamlit UI → Incident detail → AI Runbook panel

**Action:** Scroll to the Runbook section. Expand the generated runbook.

**Say:** The platform generates a runbook specific to this incident — not a generic template. It includes diagnostic checks to run first, the exact remediation steps, rollback steps if something goes wrong, and verification checks to confirm the fix took effect. Each step is annotated with the source — whether it came from your own resolved incidents, the knowledge base, or cross-client patterns. The engineer can read this, reject individual steps, or add notes before it goes anywhere near execution.

**Expected output:** Runbook renders with three sections: Pre-checks (2–3 diagnostic commands), Remediation steps (e.g. `identify runaway process`, `kill PID`, `adjust ulimit or cgroup limit`), Verification checks (e.g. `confirm CPU normalises within 2 minutes`). Each step has a source annotation. A `Validate Runbook` button is visible.

---

### Step 9: Error Tolerance Evaluation — Show the Criticality Banner (prod-standalone → Human Required)

**Screen:** Streamlit UI → Incident detail → Tolerance Evaluation panel

**Action:** Scroll to the Tolerance Evaluation section, or click the **Evaluate Tolerance** button if not yet run.

**Say:** This is a key decision gate. The platform reads `linux-lab-02`'s CMDB record: standalone host, no redundancy, criticality=2. It cross-references the SLO error budget and blast radius. The result is the amber banner you see — `Human approval required`. The system is telling the engineer: this host has no redundant peer, the error budget is not flush, and the blast radius exceeds the autonomous threshold. A human decision is mandatory here.

**Expected output:** An amber banner renders at the top of the tolerance section with text like `HUMAN APPROVAL REQUIRED — standalone host, criticality=2, blast_radius=HIGH`. Below it shows the individual inputs: `redundancy: none`, `slo_budget_remaining: 12%`, `blast_radius: HIGH`. Contrast with `linux-lab-01` (if shown inline): green banner, `AUTO-REMEDIATION PERMITTED`.

---

### Step 10: Automation Queue — Promote to Queue, Show the Governance Controls

**Screen:** Streamlit UI → Incident detail → then navigate to Automation Queue tab

**Action:** Click **Promote to Queue** in the incident detail. Then switch to the Automation Queue tab.

**Say:** Promoting the incident to the automation queue does not execute anything. It moves the remediation package — the runbook, the evidence, the tolerance decision — into a governed queue where it waits for the safety gate to run. You can see both incidents in the queue now: `linux-lab-02` in `pending_approval`, `linux-lab-01` in `pending_auto`. The queue is auditable — every state change is timestamped and attributed.

**Expected output:** Automation Queue tab shows two entries. `linux-lab-02` shows status `pending_approval`. `linux-lab-01` shows status `pending_auto`. Each entry shows the incident ID, the classified type, the assigned path, and the time it entered the queue.

---

### Step 11: Safety Gate — Run Pre-Execution Safety Gate, Explain the 6 Checks

**Screen:** Streamlit UI → Automation Queue → linux-lab-02 entry → Safety Gate panel

**Action:** Click on the `linux-lab-02` queue entry to expand it. Click **Run Safety Gate**.

**Say:** Before anything executes — even on a snapshot — the safety gate runs six independent checks. Each check consults a different evidence source: infrastructure drift from your GitOps or Terraform state, the SLO error budget from your observability stack, concurrent activity from your change management system, the change window calendar, rollback readiness, and blast radius from CMDB. All six must pass for execution to be permitted. If any check fails, the queue entry is blocked, the engineer is told exactly why, and nothing proceeds until the issue is resolved.

**Expected output:** Safety gate runs and renders six cards in a grid: `infrastructure_drift`, `error_budget_slo`, `concurrent_activity`, `change_window`, `rollback_readiness`, `blast_radius`. Each card shows a status badge (PASS / WARN / FAIL), the evidence source (e.g. `Terraform state`, `Prometheus`), and a brief note. All six show PASS for this demo. A green `Gate Passed` banner appears at the bottom.

---

### Step 12: Human Approval (Path A — linux-lab-02) — Show the Approval Button and What It Gates

**Screen:** Streamlit UI → Automation Queue → linux-lab-02 entry

**Action:** Point to the **Approve** button without clicking it yet. Explain what is gated behind it.

**Say:** The Approve button is now unlocked because the safety gate passed. Until that gate passes, this button is greyed out — clicking it has no effect. When the engineer clicks Approve, they are signing off on: this runbook, on this host, with this evidence, at this moment in time. The approval is recorded with a timestamp and the approver identity. Now watch — I will click Approve, and the platform will move to the next step.

**Action:** Click **Approve**.

**Expected output:** The queue entry status changes from `pending_approval` to `approved`. A timestamp and `Approved by: [user]` appear in the audit trail section. The **Capture Snapshot** step becomes available.

---

### Step 13: Auto-Approval (Path B — linux-lab-01) — Show That Policy Auto-Approves the Redundant System

**Screen:** Streamlit UI → Automation Queue → linux-lab-01 entry

**Action:** Click on the `linux-lab-01` queue entry to expand it.

**Say:** Compare this to `linux-lab-01`. The safety gate ran automatically — no human triggered it. Because all six checks passed and the tolerance evaluation returned `auto-remediate permitted`, the platform auto-approved this entry. The status went from `pending_auto` directly to `approved` without any human interaction. The audit trail still records every step — it just shows `Approved by: policy_engine` instead of a person's name. Full traceability, no human bottleneck.

**Expected output:** `linux-lab-01` entry shows status `approved`. Audit trail shows `Safety gate: PASS (automated)` and `Approved by: policy_engine` with a timestamp. No manual approval prompt was presented.

---

### Step 14: Drift Snapshot Captured — Show the "Capture Snapshot & Test Resolution" Step

**Screen:** Streamlit UI → Automation Queue → linux-lab-02 entry → Snapshot panel

**Action:** Click **Capture Snapshot**.

**Say:** The platform now takes a point-in-time snapshot of `linux-lab-02`'s configuration state — installed packages, running services, active connections, key config files. This is the "before" photograph. If the remediation fails or produces unexpected side effects, we have an exact record of what the system looked like before we touched it. The snapshot ID is written to the audit trail now.

**Expected output:** A snapshot is created and a snapshot ID is displayed (e.g. `snap_20260714_143200_linux-lab-02`). The panel shows the captured state items: package manifest, service list, network state. A `Snapshot captured` confirmation message appears.

---

### Step 15: Remediation Applied to Snapshot — Show the Fix Being Tested in Isolation

**Screen:** Streamlit UI → Automation Queue → linux-lab-02 entry → Snapshot Remediation panel

**Action:** Click **Apply Remediation to Snapshot** (or this may trigger automatically after capture).

**Say:** The remediation steps from the runbook are now applied to the snapshot — not to the live host. This is the isolation test. The platform runs the playbook against the snapshot and checks whether the fix produces the expected outcome without breaking anything. If this test fails, the live host is untouched and the engineer gets a clear failure report. No surprises on production.

**Expected output:** Remediation steps shown executing against the snapshot (e.g. `kill runaway_process`, `apply ulimit config`). Each step shows a status tick as it completes. A `Remediation applied to snapshot` confirmation appears when complete.

---

### Step 16: Snapshot Verification — Show the PASS Result

**Screen:** Streamlit UI → Automation Queue → linux-lab-02 entry → Snapshot Verification panel

**Action:** Click **Verify Snapshot**, or observe automatic verification if it runs immediately after remediation.

**Say:** The verification step checks the snapshot post-remediation: is the symptom gone? Are services still up? Is anything unexpected in the diff? In this case we get a PASS — the fix resolved the condition in the snapshot environment, no collateral changes were detected. Now the platform is confident it is safe to apply the same steps to the live host.

**Expected output:** Verification result shows `PASS` in green. A diff summary shows before/after state — e.g. `runaway_process: terminated`, `CPU projection: nominal`. No unexpected changes flagged. A `Safe to proceed to live execution` banner appears.

---

### Step 17: Apply to Live (Path A) / Auto-Execute (Path B) — Show the Gated Button Unlock

**Screen:** Streamlit UI → Automation Queue — both entries side by side (or toggle between them)

**Action:** For Path A (`linux-lab-02`): point to the **Apply to Live** button, which is now unlocked. For Path B (`linux-lab-01`): show that the system has already moved to `executing` automatically.

**Say:** For `linux-lab-02`, the engineer now clicks Apply to Live. The button was locked until snapshot verification passed — that is not optional, the platform enforces it. For `linux-lab-01`, the policy engine already moved it to execution as soon as the snapshot verified — the engineer did not need to be in the room for that one.

**Action:** Click **Apply to Live** for `linux-lab-02`.

**Expected output:** `linux-lab-02` status changes to `executing`. `linux-lab-01` already shows `executing` or `executed`. An Ansible execution panel opens showing live playbook output.

---

### Step 18: Ansible Execution — Show the Live Output in the Execution Panel

**Screen:** Streamlit UI → Automation Queue → Execution panel (either entry)

**Action:** Watch the Ansible output stream. Let it complete before speaking.

**Say:** This is Ansible Runner executing the validated playbook against the live host — the same steps that passed the snapshot test, in the same order. You can see the task-by-task output in real time. If any task fails, execution halts, the incident stays open, and the rollback readiness evidence we validated in the safety gate becomes the recovery path.

**Expected output:** Execution panel shows Ansible output streaming line by line: `TASK [identify runaway process] ... CHANGED`, `TASK [terminate process] ... CHANGED`, `TASK [verify CPU normalises] ... OK`. Final summary: `PLAY RECAP: linux-lab-02 — ok=5  changed=2  failed=0`. Execution status changes to `completed`.

---

### Step 19: Post-Execution Drift Verify — Show the Live System Check

**Screen:** Streamlit UI → Automation Queue → Post-execution verification panel

**Action:** Click **Verify Live System** (or observe it run automatically post-execution).

**Say:** The platform runs a post-execution drift check on the live host — comparing current state to the pre-fix snapshot to confirm the only changes are the ones we intended. If there is unexpected drift, the audit trail captures it. This step also feeds back into the resolution confidence score, so future similar incidents benefit from knowing this remediation verified cleanly.

**Expected output:** A before/after diff renders showing only the expected changes (e.g. process terminated, CPU metric now within threshold). A `Live verification: PASS` badge appears. No unexpected drift detected.

---

### Step 20: Resolution Saved + Audit Written — Show the Before/After Diff

**Screen:** Streamlit UI → Automation Queue → Audit section, or navigate to the incident and open the Audit tab

**Action:** Expand the audit trail section. Scroll through the entries.

**Say:** The audit trail is written atomically at each step — it cannot be edited after the fact. What you are seeing here is the complete chain of evidence: alert timestamp, classification, tolerance decision, safety gate results per check, approval event, snapshot ID, snapshot verification result, execution output, live verification result. This is the same package your compliance team would receive after a real production incident.

**Expected output:** Audit trail shows a chronological list of entries with timestamps: `Alert received`, `Incident created`, `Analysed — HIGH confidence`, `Promoted to queue`, `Safety gate PASSED (6/6)`, `Approved by [user]` (Path A) or `Auto-approved by policy_engine` (Path B), `Snapshot captured`, `Snapshot remediated`, `Snapshot verified PASS`, `Applied to live`, `Execution completed`, `Live verification PASS`. Each entry is immutable and timestamped.

---

### Step 21: Incident Closed — Show the Incident Moving to Resolved

**Screen:** Streamlit UI → Incidents tab

**Action:** Navigate back to the Incidents tab.

**Say:** Both incidents are now in `Resolved` state. The resolution was written automatically when the live verification passed — the engineer did not need to manually close the ticket. The incident record now carries the full resolution summary: what happened, what was done, who approved it, how long it took, and a link to the evidence package.

**Expected output:** Both incident cards show status `Resolved` with a green badge. Timestamps show the open-to-resolved duration. Clicking into either incident shows a resolution summary with a link to the audit trail and the evidence export.

---

### Step 22: Governance Report — Show MTTR, SLA Compliance, Audit Trail, Evidence Package

**Screen:** Streamlit UI → Governance tab (or export from the incident detail)

**Action:** Navigate to the Governance tab. Show the report for today's session. Point to the MTTR figure, the SLA compliance status, and the export button.

**Say:** The governance report is generated from the audit data — no manual input. MTTR for both incidents is under 15 minutes end-to-end. SLA compliance shows PASS. The evidence package covers both paths — one with human approval, one autonomous — and the audit trail is exportable for your change advisory board, your auditors, or your post-incident review. Path A shows the full human decision chain. Path B shows the policy logic that authorised autonomous action. Both are equally auditable.

**Expected output:** Governance panel shows: `Incidents resolved: 2`, `MTTR (Path A): ~12 min`, `MTTR (Path B): ~8 min`, `SLA compliance: PASS`, `Human touchpoints (Path A): 1 (approval)`, `Human touchpoints (Path B): 0`. An **Export Evidence Package** button is visible. Audit trail summary is shown inline.

---

## Key Talking Points

- **Governed, not just automated.** Every execution decision — including autonomous ones — is auditable. The system records why it acted, not just what it did. Policy-engine approvals are as traceable as human approvals.

- **The safety gate is the trust anchor.** Six independent checks, each consulting a different evidence source, run before any live execution. You can onboard your own ServiceNow, Terraform, or Prometheus data into those checks. The adapter layer is already designed for it.

- **Criticality drives the path, not the operator.** The routing decision between human approval and autonomous execution comes from CMDB attributes — tier, criticality score, redundancy type. Engineers do not decide per-incident; they set policy once. This removes the inconsistency of individual judgement calls under pressure.

- **The snapshot is the safety net.** No Ansible playbook touches a live host until it has passed an identical test against a captured snapshot. Failed fixes fail quietly in isolation, not on production.

- **Audit evidence is an output, not an afterthought.** The governance export is generated from the same data the platform uses to make decisions. There is no separate reporting layer that could diverge from what actually happened.

- **Human workload does not scale linearly with incident volume.** Path B incidents resolve end-to-end without engineer involvement. As the autonomous candidate pool grows — after three or more clean runs per candidate — a larger proportion of incidents shift to Path B, reducing the on-call load without reducing oversight.

---

## Common Questions & Answers

**Q: What stops the AI from auto-remediating something it should not?**

Three layers. First, the CMDB drives the tolerance evaluation — if a host is standalone or high-criticality, auto-remediation is prohibited regardless of confidence score. Second, the safety gate runs six independent checks and any FAIL blocks execution. Third, even for autonomous candidates the snapshot-verify step must pass before Ansible touches the live host. The autonomous threshold also requires three or more prior clean runs and a confidence score above 0.70 — new candidates always start on Path A.

**Q: We already have Ansible and ServiceNow. Do we need to rip them out?**

No. The platform wraps them. Ansible Runner executes your existing playbooks. The safety gate's evidence adapter layer reads from ServiceNow change records and your observability stack via the universal evidence contract. You bring your tooling; the platform provides the decision logic, the governance layer, and the audit trail on top.

**Q: How is this different from a traditional runbook automation tool?**

Traditional runbook automation executes scripts against triggers. This platform classifies the incident, correlates evidence, evaluates whether automation is appropriate for this specific host at this specific moment, tests the fix in isolation before touching production, and produces a governed audit record. The difference is supervised judgment, not just speed.

**Q: Who sees what? Can the AI read our incident data for training?**

Client data governance is per-tenant. For `client_a`, outbound contribution to cross-client patterns is disabled — your incident data does not leave your environment. You receive inbound peer insights from the anonymised global pool but contribute nothing to it. That policy is set in the client onboarding configuration and is auditable.

**Q: What happens when the safety gate fails?**

The queue entry is blocked. The failure card shows exactly which check failed and what evidence was missing or in conflict. The engineer is notified. Nothing executes. The incident stays open until the blocking condition is resolved — for example, a concurrent change window closes, or a CMDB record is corrected — and then the gate can be re-run. The platform does not retry automatically on a failed gate.

---

## Demo Reset Instructions

Run these steps to restore a clean state for a second run.

```bash
# 1. Archive or delete open/resolved incidents
#    Streamlit UI → Incidents → select all → Archive
#    Or: call PATCH /incidents/{id}/status {"status": "archived"} for each

# 2. Clear automation queue
#    Streamlit UI → Automation Queue → select completed/cancelled → Clear
#    Or: DELETE /automation/queue/{entry_id} for each completed entry

# 3. Destroy any open drift snapshots
#    POST /drift/destroy {"snapshot_id": "<snap_id>", "write_audit": true}
#    Or in UI: Drift Snapshots → Destroy All

# 4. Reset finding statuses (reopen for re-demo)
#    python demo/seed_findings.py   (re-seeds with open status)

# 5. Confirm evidence files are current (TTL enforced in production mode)
ls -la data/evidence/
#    If stale: re-upload via Streamlit UI → Settings → Evidence Upload
#    Or: copy from data/evidence/samples/ to data/evidence/

# 6. Restart containers if metrics need to be reset
docker restart linux-lab-01 linux-lab-02

# 7. Verify clean state
curl http://localhost:8001/health
#    Navigate to Streamlit UI — Incident Console should be empty or archived only
```

**Target state after reset:** Zero open incidents, empty automation queue, no active snapshots, all containers running, all six evidence files present and dated within the last hour.
