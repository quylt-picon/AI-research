"""Load rules/report_rules.yaml and resolve which fields apply to a job.

The YAML is the single source of truth: report_draft.py checks a report
against it in code, prompt.py renders it into the instruction Gemini reads.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

FIELD_TYPES = ("text", "enum", "boolean", "list")
CONDITION_OPS = ("equals", "not_equals", "in", "not_in")


class RulesError(ValueError):
    """The rules file is unreadable or breaks the format described in its header."""


@dataclass(frozen=True)
class FieldRule:
    key: str
    label: str
    type: str = "text"
    vi: str = ""
    options: tuple[str, ...] = ()
    item_fields: dict[str, str] = field(default_factory=dict)
    maps_to: str | None = None
    required: bool = False
    required_when: dict[str, Any] | None = None
    expect: Any = None
    waivable: bool = False
    min_chars: int = 0
    ask: str = ""
    guide: str = ""
    example: str = ""
    approver_note: str = ""
    source: str = "common"

    @property
    def conditional(self) -> bool:
        return self.required_when is not None


def condition_met(cond: dict[str, Any] | None, values: dict[str, Any]) -> bool:
    """`{field, equals|not_equals|in|not_in}` against the values recorded so far.
    A field not recorded yet never meets `equals`/`in` (nothing to ask yet)."""
    if cond is None:
        return True
    value = values.get(cond["field"])
    if "equals" in cond:
        return value == cond["equals"]
    if "not_equals" in cond:
        return value is not None and value != cond["not_equals"]
    if "in" in cond:
        return value in cond["in"]
    if "not_in" in cond:
        return value is not None and value not in cond["not_in"]
    return False


def _check_condition(cond: Any, where: str) -> None:
    if not isinstance(cond, dict) or "field" not in cond:
        raise RulesError(f"{where}: điều kiện phải có dạng {{field: ..., equals|in|...: ...}}")
    ops = [op for op in CONDITION_OPS if op in cond]
    if len(ops) != 1:
        raise RulesError(f"{where}: điều kiện cần đúng 1 toán tử trong {CONDITION_OPS}")


def _field(raw: dict[str, Any], source: str) -> FieldRule:
    where = f"{source} › {raw.get('key', '?')}"
    if not raw.get("key") or not raw.get("label"):
        raise RulesError(f"{where}: thiếu key hoặc label")
    ftype = raw.get("type", "text")
    if ftype not in FIELD_TYPES:
        raise RulesError(f"{where}: type '{ftype}' không hợp lệ ({FIELD_TYPES})")
    if ftype == "enum" and not raw.get("options"):
        raise RulesError(f"{where}: enum cần options")
    if ftype == "list" and not raw.get("item_fields"):
        raise RulesError(f"{where}: list cần item_fields")
    if raw.get("required_when") is not None:
        _check_condition(raw["required_when"], where)
    return FieldRule(
        key=str(raw["key"]),
        label=str(raw["label"]),
        type=ftype,
        vi=str(raw.get("vi", "")),
        options=tuple(str(o) for o in raw.get("options") or ()),
        item_fields={str(k): str(v) for k, v in (raw.get("item_fields") or {}).items()},
        maps_to=raw.get("maps_to"),
        required=bool(raw.get("required", False)),
        required_when=raw.get("required_when"),
        expect=raw.get("expect"),
        waivable=bool(raw.get("waivable", False)),
        min_chars=int(raw.get("min_chars", 0)),
        ask=str(raw.get("ask", "")).strip(),
        guide=str(raw.get("guide", "")).strip(),
        example=str(raw.get("example", "")).strip(),
        approver_note=str(raw.get("approver_note", "")).strip(),
        source=source,
    )


@dataclass
class RuleSet:
    raw: dict[str, Any]
    path: Path
    text: str

    @property
    def version(self) -> str:
        return str(self.raw.get("version", ""))

    @property
    def conversation(self) -> dict[str, Any]:
        return self.raw.get("conversation") or {}

    @property
    def exemption_policy(self) -> dict[str, Any]:
        return self.raw.get("exemption_policy") or {}

    @property
    def cost_items(self) -> dict[str, Any]:
        return self.raw.get("cost_items") or {"enabled": False}

    @property
    def finalize(self) -> dict[str, Any]:
        return self.raw.get("finalize") or {}

    def category(self, large: str) -> dict[str, Any]:
        return (self.raw.get("categories") or {}).get(large) or {}

    def matched_symptoms(self, large: str, small: str) -> list[dict[str, Any]]:
        return [
            s
            for s in self.category(large).get("symptoms") or []
            if any(word in (small or "") for word in s.get("match") or [])
        ]

    def fields_for(self, large: str, small: str) -> list[FieldRule]:
        """Common fields, then the category's, then each matched symptom's —
        a later field with the same key replaces the earlier one in place."""
        ordered: dict[str, FieldRule] = {}
        layers: list[tuple[str, list[dict[str, Any]]]] = [
            ("共通", self.raw.get("common_fields") or []),
            (large, self.category(large).get("fields") or []),
        ]
        for sym in self.matched_symptoms(large, small):
            layers.append((f"{large}／{'・'.join(sym.get('match') or [])}", sym.get("fields") or []))
        for source, raws in layers:
            for raw in raws:
                rule = _field(raw, source)
                ordered[rule.key] = rule
        return list(ordered.values())

    def vocabulary_for(self, large: str) -> list[str]:
        return [str(w) for w in self.category(large).get("vocabulary") or []]

    def validate(self) -> None:
        """Parse every field of every category once, so a bad edit fails on
        reload rather than in the middle of a call."""
        if not isinstance(self.raw.get("common_fields"), list):
            raise RulesError("thiếu common_fields")
        self.fields_for("", "")
        for large, cat in (self.raw.get("categories") or {}).items():
            for sym in cat.get("symptoms") or []:
                if not sym.get("match"):
                    raise RulesError(f"{large}: symptoms cần match")
            smalls = [w for s in cat.get("symptoms") or [] for w in s.get("match") or []]
            for small in [""] + smalls:
                self.fields_for(large, small)
        cost = self.cost_items
        if cost.get("enabled"):
            groups = cost.get("cost_groups") or {}
            for check in cost.get("checks") or []:
                if check.get("type") == "require_group" and check.get("group") not in groups:
                    raise RulesError(f"cost_items › {check.get('id')}: group không có trong cost_groups")
                if check.get("when") is not None:
                    _check_condition(check["when"], f"cost_items › {check.get('id')}")


def load_rules(path: Path) -> RuleSet:
    try:
        text = path.read_text(encoding="utf-8")
        raw = yaml.safe_load(text)
    except (OSError, yaml.YAMLError) as exc:
        raise RulesError(f"Không đọc được {path.name}: {exc}") from exc
    if not isinstance(raw, dict):
        raise RulesError(f"{path.name}: nội dung phải là một mapping YAML")
    rules = RuleSet(raw=raw, path=path, text=text)
    rules.validate()
    return rules


class RulesStore:
    """Holds the current rules; reload() swaps them only if the new file is valid."""

    def __init__(self, path: Path):
        self._path = path
        self._lock = threading.Lock()
        self._rules = load_rules(path)

    @property
    def current(self) -> RuleSet:
        with self._lock:
            return self._rules

    def reload(self) -> RuleSet:
        rules = load_rules(self._path)
        with self._lock:
            self._rules = rules
        return rules
