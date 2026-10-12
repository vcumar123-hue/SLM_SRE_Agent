"""Subprocess helpers for the queue-lock tests (run as: python -m tests._lock_helpers <command> ...)."""
import json
import os
import sys
import time


def hold(lock_path: str, seconds: float) -> None:
    import portalocker
    lock = portalocker.Lock(lock_path, mode="a", timeout=5, flags=portalocker.LOCK_EX | portalocker.LOCK_NB)
    lock.acquire()
    print("locked", flush=True)
    time.sleep(seconds)
    lock.release()


def execute(queue_id: str, authorization_json: str, call_log: str) -> None:
    import automation_engine as ae

    def slow_ansible(**kwargs):
        with open(call_log, "a", encoding="utf-8") as fh:
            fh.write("ansible\n")
        time.sleep(1.5)
        return {"ok": True}

    ae._import_connectors = lambda: {
        "run_ansible_playbook": slow_ansible,
        "run_kubernetes_action": lambda **k: {"ok": True},
        "run_ssh_command": lambda **k: {"ok": True},
        "run_powershell_command": lambda **k: {"ok": True},
    }
    result = ae.execute_automation(
        queue_id, executed_by="proc-%d" % os.getpid(), execution_mode="live",
        authorization=json.loads(authorization_json),
    )
    print(json.dumps({"status": result.get("status")}), flush=True)


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "hold":
        hold(sys.argv[2], float(sys.argv[3]))
    elif cmd == "execute":
        execute(sys.argv[2], sys.argv[3], sys.argv[4])
