from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Dict, Optional

# Project layout expected:
#   <project_root>/
#       automation_connectors.py
#       infra/docker-compose.yml
#       automation/inventory/hosts.ini
#       automation/playbooks/<playbook_name>
PROJECT_ROOT = Path(__file__).resolve().parent
AUTOMATION_ROOT = Path("/automation")
INFRA_ROOT = PROJECT_ROOT / "infra"
COMPOSE_FILE = INFRA_ROOT / "docker-compose.yml"
ANSIBLE_SERVICE = "local-aiops-app"
ANSIBLE_INVENTORY = AUTOMATION_ROOT / "inventory" / "lab.ini"


def _build_result(ok: bool, executor: str, **kwargs) -> Dict[str, Any]:
    result = {"ok": ok, "executor": executor}
    result.update(kwargs)
    return result


def _preflight_ansible(playbook_name: str) -> Optional[Dict[str, Any]]:
    inventory_file = ANSIBLE_INVENTORY
    playbook_file = AUTOMATION_ROOT / "playbooks" / playbook_name

    if not COMPOSE_FILE.exists():
        return _build_result(
            False,
            "ansible_runner",
            error="compose_file_not_found",
            compose_file=str(COMPOSE_FILE),
        )
    if not inventory_file.exists():
        return _build_result(
            False,
            "ansible_runner",
            error="inventory_not_found",
            inventory=str(inventory_file),
            compose_file=str(COMPOSE_FILE),
        )
    if not playbook_file.exists():
        return _build_result(
            False,
            "ansible_runner",
            error="playbook_not_found",
            playbook=str(playbook_file),
            compose_file=str(COMPOSE_FILE),
        )
    return None


def run_ansible_playbook(playbook_name: Optional[str] = None, step: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Execute an Ansible playbook inside the ansible-runner container using the compose file at:
      infra/docker-compose.yml

    Expected project layout:
      automation/
        inventory/hosts.ini
        playbooks/<playbook_name>
    """
    playbook_name = str(playbook_name or "").strip()
    if not playbook_name:
        return _build_result(False, "ansible_runner", error="missing_playbook_name")

    preflight = _preflight_ansible(playbook_name)
    if preflight is not None:
        return preflight

    cmd = [
        "ansible-playbook",
        "-i", str(ANSIBLE_INVENTORY),
        str(AUTOMATION_ROOT / "playbooks" / playbook_name),
    ]

    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=str(AUTOMATION_ROOT),
    )

    return _build_result(
        proc.returncode == 0,
        "ansible_runner",
        returncode=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
        playbook=playbook_name,
        compose_file=str(COMPOSE_FILE),
        working_directory=str(AUTOMATION_ROOT),
        service=ANSIBLE_SERVICE,
        step=step or {},
    )


def run_kubernetes_action(action: str, step: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Minimal Kubernetes demo executor.
    Reads namespace/deployment from the step if present.
    """
    step = step or {}
    namespace = str(step.get("namespace") or "default")
    deployment = str(step.get("deployment") or "demo-app")

    if action == "restart_pod":
        cmd = ["kubectl", "rollout", "restart", "deployment", deployment, "-n", namespace]
    elif action == "scale_deployment":
        replicas = str(step.get("replicas") or "2")
        cmd = ["kubectl", "scale", "deployment", deployment, f"--replicas={replicas}", "-n", namespace]
    else:
        return _build_result(False, "kubernetes", error=f"unsupported_action:{action}", step=step)

    proc = subprocess.run(cmd, capture_output=True, text=True)
    return _build_result(
        proc.returncode == 0,
        "kubernetes",
        action=action,
        namespace=namespace,
        deployment=deployment,
        returncode=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
        step=step,
    )


def run_ssh_command(step: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Placeholder SSH executor for future live runs.
    """
    step = step or {}
    host = step.get("host") or "unknown-host"
    command = step.get("command") or "echo SSH command placeholder"
    return _build_result(
        True,
        "ssh",
        mode="placeholder",
        host=host,
        command=command,
        message="SSH connector placeholder. Replace with Paramiko or OpenSSH execution later.",
        step=step,
    )


def run_powershell_command(step: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Placeholder PowerShell/WinRM executor for future live runs.
    """
    step = step or {}
    host = step.get("host") or "windows-host"
    command = step.get("command") or 'Write-Output "PowerShell placeholder"'
    return _build_result(
        True,
        "powershell",
        mode="placeholder",
        host=host,
        command=command,
        message="PowerShell connector placeholder. Replace with pypsrp or WinRM execution later.",
        step=step,
    )

