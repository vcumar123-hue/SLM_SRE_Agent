from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class ResolutionRecord:
    resolution_id: str
    classification: str
    root_cause: str
    validated_checks: List[str]
    validated_actions: List[str]
    engineer_confirmed: bool
    confirmed_by: str
    success_notes: str
    automation_ready: bool
    linked_candidate_id: str
    linked_playbook_id: str
    created_at: str = ""
    updated_at: str = ""


@dataclass
class PlaybookRecord:
    playbook_id: str
    name: str
    classification: str
    description: str
    steps: List[str]
    scripts: List[str]
    approval_mode: str
    rollback_available: bool
    risk_tier: str
    enabled: bool = True
    created_at: str = ""
    updated_at: str = ""


class ResolutionEngine:
    def __init__(
        self,
        data_dir: Optional[str | Path] = None,
        resolutions_filename: str = "resolutions.json",
        playbooks_filename: str = "playbooks.json",
    ) -> None:
        base_dir = Path(data_dir) if data_dir else Path(__file__).resolve().parent / "data"
        self.data_dir = base_dir
        self.resolutions_path = self.data_dir / resolutions_filename
        self.playbooks_path = self.data_dir / playbooks_filename

        self._resolutions: List[ResolutionRecord] = []
        self._playbooks: List[PlaybookRecord] = []
        self._resolutions_by_classification: Dict[str, List[ResolutionRecord]] = {}
        self._playbooks_by_classification: Dict[str, List[PlaybookRecord]] = {}
        self._playbooks_by_id: Dict[str, PlaybookRecord] = {}
        self.reload()

    def reload(self) -> None:
        self._resolutions = self._load_resolutions()
        self._playbooks = self._load_playbooks()
        self._resolutions_by_classification.clear()
        self._playbooks_by_classification.clear()
        self._playbooks_by_id.clear()

        for resolution in self._resolutions:
            key = self._normalize_classification(resolution.classification)
            if key:
                self._resolutions_by_classification.setdefault(key, []).append(resolution)

        for playbook in self._playbooks:
            key = self._normalize_classification(playbook.classification)
            if key:
                self._playbooks_by_classification.setdefault(key, []).append(playbook)
                self._playbooks_by_id[playbook.playbook_id] = playbook

    def resolve_for_classification(self, classification: str) -> Dict[str, Any]:
        normalized = self._normalize_classification(classification)
        resolution = self.get_best_resolution(normalized)
        playbook = self.get_best_playbook(normalized, resolution)
        return {
            "linked_resolution": asdict(resolution) if resolution else None,
            "linked_playbook": asdict(playbook) if playbook else None,
            "resolution_confirmed_by": resolution.confirmed_by if resolution else None,
            "playbook_available": playbook is not None,
            "resolution_automation_ready": bool(resolution.automation_ready if resolution else False),
        }

    def get_resolution_by_classification(self, classification: str) -> Optional[Dict[str, Any]]:
        resolution = self.get_best_resolution(self._normalize_classification(classification))
        return asdict(resolution) if resolution else None

    def get_playbook_by_classification(self, classification: str) -> Optional[Dict[str, Any]]:
        playbook = self.get_best_playbook(self._normalize_classification(classification), None)
        return asdict(playbook) if playbook else None

    def get_best_resolution(self, classification: str) -> Optional[ResolutionRecord]:
        matches = self._resolutions_by_classification.get(classification, [])
        if not matches:
            return None
        ranked = sorted(
            matches,
            key=lambda r: (1 if r.engineer_confirmed else 0, r.updated_at or "", r.created_at or "", r.resolution_id),
            reverse=True,
        )
        return ranked[0]

    def get_best_playbook(
        self,
        classification: str,
        resolution: Optional[ResolutionRecord] = None,
    ) -> Optional[PlaybookRecord]:
        if resolution and resolution.linked_playbook_id:
            playbook = self._playbooks_by_id.get(resolution.linked_playbook_id)
            if playbook and playbook.enabled:
                return playbook

        matches = [p for p in self._playbooks_by_classification.get(classification, []) if p.enabled]
        if not matches:
            return None
        ranked = sorted(
            matches,
            key=lambda p: (1 if p.enabled else 0, p.updated_at or "", p.created_at or "", p.playbook_id),
            reverse=True,
        )
        return ranked[0]

    def _load_resolutions(self) -> List[ResolutionRecord]:
        raw = self._load_json_array(self.resolutions_path)
        records: List[ResolutionRecord] = []
        for item in raw:
            classification = self._normalize_classification(item.get("classification", ""))
            resolution_id = str(item.get("resolution_id", "")).strip()
            if not classification or not resolution_id:
                continue
            records.append(
                ResolutionRecord(
                    resolution_id=resolution_id,
                    classification=classification,
                    root_cause=str(item.get("root_cause", "")).strip(),
                    validated_checks=self._ensure_str_list(item.get("validated_checks")),
                    validated_actions=self._ensure_str_list(item.get("validated_actions")),
                    engineer_confirmed=bool(item.get("engineer_confirmed", False)),
                    confirmed_by=str(item.get("confirmed_by", "")).strip(),
                    success_notes=str(item.get("success_notes", "")).strip(),
                    automation_ready=bool(item.get("automation_ready", False)),
                    linked_candidate_id=str(item.get("linked_candidate_id", "")).strip(),
                    linked_playbook_id=str(item.get("linked_playbook_id", "")).strip(),
                    created_at=str(item.get("created_at", "")).strip(),
                    updated_at=str(item.get("updated_at", "")).strip(),
                )
            )
        return records

    def _load_playbooks(self) -> List[PlaybookRecord]:
        raw = self._load_json_array(self.playbooks_path)
        records: List[PlaybookRecord] = []
        for item in raw:
            classification = self._normalize_classification(item.get("classification", ""))
            playbook_id = str(item.get("playbook_id", "")).strip()
            name = str(item.get("name", "")).strip()
            if not classification or classification == "string":
                continue
            if not playbook_id or not name or name == "string":
                continue
            records.append(
                PlaybookRecord(
                    playbook_id=playbook_id,
                    name=name,
                    classification=classification,
                    description=str(item.get("description", "")).strip(),
                    steps=self._ensure_str_list(item.get("steps")),
                    scripts=self._ensure_str_list(item.get("scripts")),
                    approval_mode=str(item.get("approval_mode", "manual_approval_required")).strip(),
                    rollback_available=bool(item.get("rollback_available", False)),
                    risk_tier=str(item.get("risk_tier", "medium")).strip(),
                    enabled=bool(item.get("enabled", True)),
                    created_at=str(item.get("created_at", "")).strip(),
                    updated_at=str(item.get("updated_at", "")).strip(),
                )
            )
        return records

    @staticmethod
    def _normalize_classification(value: str) -> str:
        return str(value or "").strip().lower()

    @staticmethod
    def _ensure_str_list(value: Any) -> List[str]:
        if not isinstance(value, list):
            return []
        return [str(v).strip() for v in value if str(v).strip()]

    @staticmethod
    def _load_json_array(path: Path) -> List[Dict[str, Any]]:
        if not path.exists():
            logger.warning("JSON file not found: %s", path)
            return []
        try:
            with path.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            logger.exception("Failed to load JSON file: %s", path)
            return []
        if not isinstance(data, list):
            return []
        return [item for item in data if isinstance(item, dict)]


_engine_singleton: Optional[ResolutionEngine] = None


def get_resolution_engine(data_dir: Optional[str | Path] = None) -> ResolutionEngine:
    global _engine_singleton
    if _engine_singleton is None:
        _engine_singleton = ResolutionEngine(data_dir=data_dir)
    return _engine_singleton
