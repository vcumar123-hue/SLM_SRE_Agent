"""
snapshot_engine.py — Drift Management snapshot engine.

Modes:
  lab   — captures state from Docker lab containers (linux-lab-01, linux-lab-02, network-lab-01)
           via subprocess docker exec commands
  cmdb  — reads CI configuration state from ServiceNow CMDB (read-only, no execution)
  dry   — simulates capture and remediation without touching anything

Lifecycle:
  capture → remediate → verify → destroy (writes audit record)
                                └─ fail   → keep snapshot, alert
"""

from __future__ import annotations

import json
import logging
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Project root — two levels up from tools/snapshot_engine.py
ROOT = Path(__file__).resolve().parent.parent

SNAPSHOT_DIR = ROOT / "data" / "drift" / "snapshots"
AUDIT_DIR = ROOT / "data" / "drift" / "audit"

LAB_CONTAINERS: list[str] = ["linux-lab-01", "linux-lab-02", "network-lab-01"]

# Config files whose checksums are captured
_CHECKSUM_FILES = ["/etc/hosts", "/etc/resolv.conf", "/etc/ssh/sshd_config"]


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _timestamp_tag() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


class SnapshotEngine:
    """Manages CI state snapshots for drift detection and remediation."""

    def __init__(self) -> None:
        SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        AUDIT_DIR.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Public API — Lifecycle
    # ------------------------------------------------------------------

    def capture(
        self,
        ci_name: str,
        mode: str = "lab",
        container: str | None = None,
        notes: str = "",
    ) -> dict:
        """
        Capture current state for ci_name.

        Parameters
        ----------
        ci_name:   CI identifier (also used as container name when mode='lab' and
                   container is None).
        mode:      'lab' | 'cmdb' | 'dry'
        container: Docker container name override (lab mode only).
        notes:     Free-text annotation stored with the snapshot.

        Returns the full snapshot dict.
        """
        mode = mode.lower()
        if mode not in ("lab", "cmdb", "dry"):
            raise ValueError(f"Invalid mode '{mode}'. Must be 'lab', 'cmdb', or 'dry'.")

        ts = _timestamp_tag()
        snapshot_id = f"snap_{ts}_{ci_name}"
        effective_container = container or ci_name

        logger.info("Capturing snapshot %s (mode=%s)", snapshot_id, mode)

        if mode == "lab":
            state = self._capture_lab(ci_name, effective_container)
        elif mode == "cmdb":
            state = self._capture_cmdb(ci_name)
        else:
            state = self._capture_dry(ci_name)

        snapshot: dict = {
            "snapshot_id": snapshot_id,
            "ci_name": ci_name,
            "mode": mode,
            "container": effective_container if mode == "lab" else None,
            "status": "active",
            "captured_at": _now_iso(),
            "notes": notes,
            "state": state,
            "post_state": None,
            "remediation": {
                "applied_at": None,
                "playbook": None,
                "changes": [],
                "result": None,
            },
            "verification": {
                "verified_at": None,
                "result": None,
                "checks": [],
                "diff": None,
            },
        }

        self._save_snapshot(snapshot)
        logger.info("Snapshot %s saved", snapshot_id)
        return snapshot

    def apply_remediation(
        self,
        snapshot_id: str,
        playbook_name: str,
        changes: list[dict],
    ) -> dict:
        """
        Apply remediation to a previously captured snapshot.

        For lab mode: runs an Ansible playbook via docker exec.
        For cmdb mode: records planned changes (read-only).
        For dry mode: records simulated changes.

        Returns the updated snapshot dict.
        """
        snapshot = self._load_snapshot_or_raise(snapshot_id)

        if snapshot["status"] != "active":
            raise RuntimeError(
                f"Snapshot {snapshot_id} is not active (status={snapshot['status']}). "
                "Cannot apply remediation."
            )

        mode = snapshot["mode"]
        container = snapshot.get("container") or snapshot["ci_name"]

        logger.info("Applying remediation to %s (mode=%s, playbook=%s)", snapshot_id, mode, playbook_name)

        if mode == "lab":
            result = self._run_ansible_playbook(container, playbook_name)
            remediation_result = {
                "rc": result["rc"],
                "stdout": result["stdout"][:2000],
                "stderr": result["stderr"][:500],
                "status": "success" if result["rc"] == 0 else "failed",
            }
        elif mode == "cmdb":
            logger.info("CMDB mode: recording planned changes only (no execution)")
            remediation_result = {
                "status": "planned",
                "note": "CMDB mode — no execution; changes recorded as planned.",
                "planned_changes": changes,
            }
        else:  # dry
            remediation_result = {
                "status": "simulated",
                "note": "Dry-run mode — no real changes applied.",
                "simulated_changes": changes,
            }

        snapshot["remediation"].update({
            "applied_at": _now_iso(),
            "playbook": playbook_name,
            "changes": changes,
            "result": remediation_result,
        })
        snapshot["status"] = "remediated"
        self._save_snapshot(snapshot)
        return snapshot

    def verify(self, snapshot_id: str) -> dict:
        """
        Verify the CI state after remediation.

        For lab mode: re-captures state and compares with original.
        For cmdb/dry mode: simulated pass.

        Returns the updated snapshot dict.
        """
        snapshot = self._load_snapshot_or_raise(snapshot_id)
        mode = snapshot["mode"]
        container = snapshot.get("container") or snapshot["ci_name"]
        checks: list[dict] = []

        logger.info("Verifying snapshot %s (mode=%s)", snapshot_id, mode)

        if mode == "lab":
            post_state = self._capture_lab(snapshot["ci_name"], container)
            snapshot["post_state"] = post_state
            diff = self._diff_states(snapshot["state"], post_state)

            # Check 1: critical services unchanged
            orig_services = set(snapshot["state"].get("services", []))
            new_services = set(post_state.get("services", []))
            services_ok = orig_services.issubset(new_services)
            checks.append({
                "check": "critical_services_running",
                "passed": services_ok,
                "detail": f"Missing services: {orig_services - new_services}" if not services_ok else "All original services running",
            })

            # Check 2: ports unchanged
            orig_ports = set(snapshot["state"].get("ports", []))
            new_ports = set(post_state.get("ports", []))
            ports_ok = orig_ports.issubset(new_ports)
            checks.append({
                "check": "ports_unchanged",
                "passed": ports_ok,
                "detail": f"Missing ports: {orig_ports - new_ports}" if not ports_ok else "All original ports still open",
            })

            # Check 3: dmesg errors (last 10 lines)
            dmesg_out = self._docker_exec(container, "dmesg --level=err,crit,alert,emerg -T")
            dmesg_lines = [ln for ln in dmesg_out.strip().splitlines() if ln][-10:]
            new_errors = len(dmesg_lines)
            checks.append({
                "check": "no_new_dmesg_errors",
                "passed": new_errors == 0,
                "detail": f"{new_errors} error(s) in dmesg" if new_errors else "No dmesg errors",
                "sample": dmesg_lines[:3] if dmesg_lines else [],
            })

            overall = "pass" if (services_ok and ports_ok) else "fail"
            snapshot["verification"].update({
                "verified_at": _now_iso(),
                "result": overall,
                "checks": checks,
                "diff": diff,
            })

        else:
            # cmdb / dry — simulated pass
            checks.append({
                "check": "simulated_verification",
                "passed": True,
                "detail": f"Mode '{mode}' — verification simulated as pass",
            })
            snapshot["verification"].update({
                "verified_at": _now_iso(),
                "result": "pass",
                "checks": checks,
                "diff": None,
            })

        if snapshot["verification"]["result"] == "fail":
            snapshot["status"] = "verify-failed"
            logger.warning("Snapshot %s verification FAILED — snapshot retained for investigation", snapshot_id)
        else:
            snapshot["status"] = "verified"

        self._save_snapshot(snapshot)
        return snapshot

    def destroy(self, snapshot_id: str, write_audit: bool = True) -> dict:
        """
        Destroy a snapshot after recording an audit entry.

        If write_audit is True, writes audit JSON to AUDIT_DIR.
        Returns the audit record.
        """
        snapshot = self._load_snapshot_or_raise(snapshot_id)
        snap_path = SNAPSHOT_DIR / f"{snapshot_id}.json"

        before_state = snapshot.get("state", {})
        after_state = snapshot.get("post_state")
        drift_summary = self._diff_states(before_state, after_state) if after_state else {"note": "no post-state captured"}

        audit: dict = {
            "snapshot_id": snapshot_id,
            "ci_name": snapshot["ci_name"],
            "mode": snapshot["mode"],
            "captured_at": snapshot["captured_at"],
            "remediation_applied": snapshot["remediation"].get("applied_at") is not None,
            "verify_result": snapshot["verification"].get("result"),
            "destroyed_at": _now_iso(),
            "before_state": before_state,
            "after_state": after_state,
            "drift_detected": drift_summary,
            "notes": snapshot.get("notes", ""),
        }

        if write_audit:
            audit_path = AUDIT_DIR / f"{snapshot_id}_audit.json"
            try:
                with audit_path.open("w", encoding="utf-8") as fh:
                    json.dump(audit, fh, indent=2, default=str)
                logger.info("Audit record written to %s", audit_path)
            except OSError as exc:
                logger.error("Failed to write audit record: %s", exc)

        try:
            snap_path.unlink(missing_ok=True)
            logger.info("Snapshot %s destroyed", snapshot_id)
        except OSError as exc:
            logger.error("Failed to delete snapshot file %s: %s", snap_path, exc)

        return audit

    def list_snapshots(self) -> list[dict]:
        """Return summary metadata for all active snapshots, newest first."""
        summaries: list[dict] = []
        for snap_file in sorted(SNAPSHOT_DIR.glob("snap_*.json"), reverse=True):
            try:
                with snap_file.open("r", encoding="utf-8") as fh:
                    snap = json.load(fh)
                summaries.append({
                    "snapshot_id": snap.get("snapshot_id"),
                    "ci_name": snap.get("ci_name"),
                    "mode": snap.get("mode"),
                    "status": snap.get("status"),
                    "captured_at": snap.get("captured_at"),
                    "notes": snap.get("notes", ""),
                    "verify_result": snap.get("verification", {}).get("result"),
                })
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("Could not read snapshot file %s: %s", snap_file, exc)
        return summaries

    def get_snapshot(self, snapshot_id: str) -> dict | None:
        """Load and return a specific snapshot, or None if not found."""
        snap_path = SNAPSHOT_DIR / f"{snapshot_id}.json"
        if not snap_path.exists():
            return None
        try:
            with snap_path.open("r", encoding="utf-8") as fh:
                return json.load(fh)
        except (json.JSONDecodeError, OSError) as exc:
            logger.error("Failed to load snapshot %s: %s", snapshot_id, exc)
            return None

    def list_audit(self) -> list[dict]:
        """Return all audit records sorted newest first."""
        records: list[dict] = []
        for audit_file in AUDIT_DIR.glob("*_audit.json"):
            try:
                with audit_file.open("r", encoding="utf-8") as fh:
                    records.append(json.load(fh))
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("Could not read audit file %s: %s", audit_file, exc)

        records.sort(key=lambda r: r.get("destroyed_at", ""), reverse=True)
        return records

    # ------------------------------------------------------------------
    # Private — capture helpers
    # ------------------------------------------------------------------

    def _capture_lab(self, ci_name: str, container: str) -> dict:
        """Capture live state from a Docker lab container."""
        logger.debug("Capturing lab state from container '%s'", container)

        services_raw = self._docker_exec(
            container,
            "systemctl list-units --type=service --state=running --no-pager --no-legend",
        )
        services = self._parse_service_list(services_raw)

        ports_raw = self._docker_exec(container, "ss -tlnp")
        ports = self._parse_port_list(ports_raw)

        disk = self._docker_exec(container, "df -h")
        memory = self._docker_exec(container, "free -m")
        load = self._docker_exec(container, "uptime")
        os_info = self._docker_exec(container, "cat /etc/os-release")

        checksum_raw = self._docker_exec(
            container,
            f"md5sum {' '.join(_CHECKSUM_FILES)}",
        )
        config_checksums = self._parse_checksums(checksum_raw)

        return {
            "services": services,
            "ports": ports,
            "disk": disk,
            "memory": memory,
            "load": load,
            "os_info": os_info,
            "config_checksums": config_checksums,
        }

    def _capture_cmdb(self, ci_name: str) -> dict:
        """Read CI state from ServiceNow CMDB (read-only)."""
        # Import here to avoid circular imports at module load time
        try:
            from adapters.servicenow_cmdb import get_adapter
            adapter = get_adapter()
            ci_profile = adapter.get_with_fallback(ci_name)
            relationships: list[dict] = []
            if ci_profile and ci_profile.get("sys_id"):
                relationships = adapter.get_ci_relationships(ci_profile["sys_id"])
        except Exception as exc:
            logger.warning("CMDB capture failed for '%s': %s", ci_name, exc)
            ci_profile = None
            relationships = []

        return {
            "ci_profile": ci_profile,
            "relationships": relationships,
            "note": "CMDB read-only snapshot",
        }

    def _capture_dry(self, ci_name: str) -> dict:
        """Return a synthetic state dict for dry-run testing."""
        return {
            "mode": "dry-run",
            "ci_name": ci_name,
            "services": ["sshd.service", "cron.service", "rsyslog.service"],
            "ports": ["0.0.0.0:22", "0.0.0.0:443"],
            "disk": "Simulated — df not executed",
            "memory": "Simulated — free not executed",
            "load": "Simulated — uptime not executed",
            "os_info": "Simulated — /etc/os-release not read",
            "config_checksums": {
                "/etc/hosts": "dry-run-checksum",
                "/etc/resolv.conf": "dry-run-checksum",
                "/etc/ssh/sshd_config": "dry-run-checksum",
            },
            "note": "Dry-run: no real commands executed",
        }

    # ------------------------------------------------------------------
    # Private — execution helpers
    # ------------------------------------------------------------------

    def _docker_exec(self, container: str, cmd: str) -> str:
        """
        Run a command inside a Docker container via docker exec.

        Returns stdout on success, or an error description string on failure.
        """
        try:
            result = subprocess.run(
                ["docker", "exec", container] + cmd.split(),
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode != 0:
                logger.debug(
                    "docker exec '%s' in '%s' exited %d: %s",
                    cmd, container, result.returncode, result.stderr.strip(),
                )
                return result.stdout or f"[exit {result.returncode}] {result.stderr.strip()}"
            return result.stdout
        except subprocess.TimeoutExpired:
            logger.warning("docker exec timed out: container=%s cmd=%s", container, cmd)
            return f"[timeout] Command '{cmd}' timed out after 15s"
        except FileNotFoundError:
            logger.error("'docker' not found in PATH — cannot exec into container '%s'", container)
            return "[error] docker binary not found in PATH"
        except OSError as exc:
            logger.error("docker exec failed: %s", exc)
            return f"[error] {exc}"

    def _run_ansible_playbook(self, container: str, playbook_name: str) -> dict:
        """
        Run an Ansible playbook via the aiops-ansible-runner container.

        Falls back to a simple connectivity check if the playbook is not found.
        """
        primary_cmd = [
            "docker", "exec", "aiops-ansible-runner",
            "ansible-playbook",
            f"/runner/project/{playbook_name}",
            "-i", f"{container},",
            "--connection=docker",
        ]
        try:
            result = subprocess.run(
                primary_cmd,
                capture_output=True,
                text=True,
                timeout=120,
            )
            return {
                "rc": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "playbook": playbook_name,
                "method": "ansible-playbook",
            }
        except subprocess.TimeoutExpired:
            return {
                "rc": 1,
                "stdout": "",
                "stderr": f"Ansible playbook '{playbook_name}' timed out after 120s",
                "playbook": playbook_name,
                "method": "ansible-playbook",
            }
        except Exception as exc:
            logger.warning("Ansible playbook failed (%s) — falling back to inline check", exc)

        # Fallback: simple ping / echo via docker exec
        fallback_result = subprocess.run(
            ["docker", "exec", container, "echo", "aiops-remediation-probe"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        return {
            "rc": fallback_result.returncode,
            "stdout": fallback_result.stdout,
            "stderr": fallback_result.stderr,
            "playbook": playbook_name,
            "method": "inline-fallback",
            "note": "Ansible runner unavailable; inline command used as fallback",
        }

    # ------------------------------------------------------------------
    # Private — state parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_service_list(raw: str) -> list[str]:
        """Extract service unit names from systemctl list-units output."""
        services: list[str] = []
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith("●") or line.startswith("UNIT"):
                continue
            parts = line.split()
            if parts and parts[0].endswith(".service"):
                services.append(parts[0])
        return services

    @staticmethod
    def _parse_port_list(raw: str) -> list[str]:
        """Extract listening addresses from ss -tlnp output."""
        ports: list[str] = []
        for line in raw.splitlines():
            if line.startswith("LISTEN") or "LISTEN" in line:
                parts = line.split()
                # Column 4 is local address in ss output
                if len(parts) >= 5:
                    ports.append(parts[4])
        return ports

    @staticmethod
    def _parse_checksums(raw: str) -> dict[str, str]:
        """Parse md5sum output into {filename: checksum}."""
        result: dict[str, str] = {}
        for line in raw.splitlines():
            parts = line.split(None, 1)
            if len(parts) == 2:
                checksum, filename = parts
                result[filename.strip()] = checksum.strip()
        return result

    # ------------------------------------------------------------------
    # Private — diff & persistence
    # ------------------------------------------------------------------

    @staticmethod
    def _diff_states(before: dict, after: dict) -> dict:
        """
        Compare two state dicts (shallow keys) and return a diff summary.

        Returns:
          {
            "changed": [{"key": k, "before": v1, "after": v2}, ...],
            "added":   [{"key": k, "value": v}, ...],
            "removed": [{"key": k, "value": v}, ...],
            "unchanged_count": N,
          }
        """
        changed: list[dict] = []
        added: list[dict] = []
        removed: list[dict] = []
        unchanged_count = 0

        all_keys = set(before.keys()) | set(after.keys())
        for key in sorted(all_keys):
            if key not in before:
                added.append({"key": key, "value": after[key]})
            elif key not in after:
                removed.append({"key": key, "value": before[key]})
            elif before[key] != after[key]:
                changed.append({
                    "key": key,
                    "before": _summarise(before[key]),
                    "after": _summarise(after[key]),
                })
            else:
                unchanged_count += 1

        return {
            "changed": changed,
            "added": added,
            "removed": removed,
            "unchanged_count": unchanged_count,
        }

    def _save_snapshot(self, snapshot: dict) -> None:
        """Persist a snapshot to SNAPSHOT_DIR/{snapshot_id}.json."""
        snap_path = SNAPSHOT_DIR / f"{snapshot['snapshot_id']}.json"
        try:
            with snap_path.open("w", encoding="utf-8") as fh:
                json.dump(snapshot, fh, indent=2, default=str)
        except OSError as exc:
            logger.error("Failed to save snapshot %s: %s", snapshot["snapshot_id"], exc)
            raise

    def _load_snapshot_or_raise(self, snapshot_id: str) -> dict:
        """Load a snapshot and raise FileNotFoundError if missing."""
        snap = self.get_snapshot(snapshot_id)
        if snap is None:
            raise FileNotFoundError(f"Snapshot '{snapshot_id}' not found in {SNAPSHOT_DIR}")
        return snap


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def _summarise(value: Any, max_len: int = 200) -> Any:
    """Truncate long string values for diff readability."""
    if isinstance(value, str) and len(value) > max_len:
        return value[:max_len] + f"… [{len(value)} chars total]"
    if isinstance(value, list) and len(value) > 20:
        return value[:20] + [f"… ({len(value)} items total)"]
    return value
