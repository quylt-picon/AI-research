"""The report being built during one call, and the checklist that decides
whether it is complete.

Gemini only talks and extracts; every "is this enough?" decision is made
here, in code, from the rules — the same split approve-helper makes
("Code decides the case and composes every text").
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from app.services.rules import FieldRule, RuleSet, condition_met

OPEN = ("missing", "needs_detail")
COST_GROUP_KEYS = ("material", "labor", "support", "adjustment")
# approve-helper's English cost-group names, for the estimation-evaluation payload.
COST_GROUP_EN = {
    "material": "Material Cost",
    "labor": "Labor Cost",
    "support": "Support Cost",
    "adjustment": "Adjustment Cost",
}
_TRUE = {"true", "yes", "はい", "あり", "有", "有り", "済", "済み", "した", "撮影済み", "1"}
_FALSE = {"false", "no", "いいえ", "なし", "無", "無し", "未", "していない", "未撮影", "0"}


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass
class Requirement:
    id: str
    label: str
    status: str  # ok | missing | needs_detail | waived
    issue: str
    ask: str
    waivable: bool
    kind: str  # field | cost


class _SafeDict(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def _fmt(template: str, **kw: Any) -> str:
    return " ".join((template or "").split()).replace("。 ", "。").format_map(_SafeDict(kw))


def _is_empty(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def _match_option(value: Any, options: tuple[str, ...]) -> str | None:
    """Exact option, else the one option the value is a prefix/part of."""
    text = re.sub(r"\s", "", str(value))
    for opt in options:
        if text == re.sub(r"\s", "", opt):
            return opt
    hits = [o for o in options if text and (text in o or o.split("（")[0] in text)]
    return hits[0] if len(hits) == 1 else None


def _to_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    return None


def _to_number(value: Any) -> int | float | None:
    """Ints stay ints (32000, not 32000.0) — amounts go to approve-helper as-is."""
    if value is None or value == "" or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        try:
            number = float(re.sub(r"[,，円¥\s]", "", str(value)))
        except ValueError:
            return None
    return int(number) if number.is_integer() else number


def _num_text(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{int(value):,}" if float(value).is_integer() else f"{value:,}"


class ReportDraft:
    def __init__(self, job: dict[str, Any], rules: RuleSet):
        self.job = job
        self.rules = rules
        self.fields: list[FieldRule] = rules.fields_for(job["large"], job["small"])
        self._by_key = {f.key: f for f in self.fields}
        self.values: dict[str, Any] = {}
        self.cost_items: list[dict[str, Any]] = []
        self.exemptions: dict[str, dict[str, Any]] = {}
        self.transcript: list[dict[str, Any]] = []
        self.tool_log: list[dict[str, Any]] = []
        self.started_at = now_iso()
        self.resumed = False
        self.finalized_report: dict[str, Any] | None = None

    # ------------------------------------------------------------ updates

    def _coerce(self, rule: FieldRule, value: Any) -> tuple[Any, str | None]:
        if rule.type == "text":
            text = str(value).strip()
            return (text, None) if text else (None, "空の値")
        if rule.type == "enum":
            opt = _match_option(value, rule.options)
            if opt is None:
                return None, f"選択肢 {list(rule.options)} のいずれかで記録してください"
            return opt, None
        if rule.type == "boolean":
            b = _to_bool(value)
            return (b, None) if b is not None else (None, "true / false で記録してください")
        # list
        rows = value if isinstance(value, list) else [value]
        cols = list(rule.item_fields)
        out = []
        for row in rows:
            if isinstance(row, str):
                row = {cols[0]: row}
            if not isinstance(row, dict):
                continue
            clean = {c: str(row[c]).strip() for c in cols if not _is_empty(row.get(c))}
            if clean:
                out.append(clean)
        return (out, None) if out else (None, f"各行に {cols} を含めてください")

    def set_values(self, updates: dict[str, Any]) -> tuple[list[str], list[dict[str, str]]]:
        saved, rejected = [], []
        for key, value in (updates or {}).items():
            rule = self._by_key.get(key)
            if rule is None:
                rejected.append({"key": key, "reason": "この案件の報告項目ではありません"})
                continue
            if _is_empty(value):
                self.values.pop(key, None)
                saved.append(key)
                continue
            coerced, error = self._coerce(rule, value)
            if error:
                rejected.append({"key": key, "reason": error})
                continue
            self.values[key] = coerced
            saved.append(key)
        self._prune_exemptions()
        return saved, rejected

    def set_cost_items(self, items: list[dict[str, Any]]) -> list[dict[str, str]]:
        """Replace the whole list (the model always sends every line)."""
        rejected, clean = [], []
        for row in items or []:
            label = str(row.get("label") or "").strip()
            group = str(row.get("cost_group") or "").strip()
            if not label:
                rejected.append({"item": str(row), "reason": "label が空です"})
                continue
            if group not in COST_GROUP_KEYS:
                rejected.append({"item": label, "reason": f"cost_group は {list(COST_GROUP_KEYS)}"})
                continue
            quantity = _to_number(row.get("quantity"))
            clean.append(
                {
                    "label": label,
                    "cost_group": group,
                    "quantity": quantity if quantity is not None else 1,
                    "unit": str(row.get("unit") or "").strip(),
                    "amount": _to_number(row.get("amount")),
                    "note": str(row.get("note") or "").strip(),
                }
            )
        self.cost_items = clean
        self._prune_exemptions()
        return rejected

    def exempt(self, requirement_id: str, reason: str, reason_is_specific: bool = True) -> dict[str, Any]:
        reqs = {r.id: r for r in self.requirements(apply_exemptions=False)}
        req = reqs.get(requirement_id)
        if req is None:
            open_ids = [r.id for r in reqs.values() if r.status in OPEN]
            return {"ok": False, "error": "その requirement_id はありません", "open_ids": open_ids}
        if req.status == "ok":
            return {"ok": False, "error": "この項目はすでに充足しています。免除は不要です。"}
        if not req.waivable:
            return {
                "ok": False,
                "error": "この項目は免除できません。「だいたいで構いません」と伝え、わかる範囲で答えてもらってください。",
            }
        if not (reason or "").strip():
            return {"ok": False, "error": "理由が空です。作業者に理由を聞いてください。"}
        self.exemptions[requirement_id] = {
            "requirement_id": requirement_id,
            "label": req.label,
            "reason": reason.strip(),
            "reason_is_specific": bool(reason_is_specific),
            "recorded_at": now_iso(),
        }
        return {"ok": True}

    def _prune_exemptions(self) -> None:
        """Drop an exemption once its requirement is met or no longer applies."""
        still_open = {r.id for r in self.requirements(apply_exemptions=False) if r.status in OPEN}
        for rid in list(self.exemptions):
            if rid not in still_open:
                del self.exemptions[rid]

    # ------------------------------------------------------------ checklist

    def applies(self, rule: FieldRule) -> bool:
        return rule.required or (rule.conditional and condition_met(rule.required_when, self.values))

    def _field_status(self, rule: FieldRule) -> tuple[str, str]:
        value = self.values.get(rule.key)
        if _is_empty(value):
            return "missing", "未入力"
        if rule.type == "text" and rule.min_chars and len(value) < rule.min_chars:
            return "needs_detail", f"「{value}」では具体性が足りません。もう少し詳しく聞いてください。"
        if rule.expect is not None and value != rule.expect:
            return "missing", f"回答が「{self.display_value(rule, value)}」でした。できなかった理由を聞いて免除として記録してください。"
        return "ok", ""

    def _cost_requirements(self) -> list[Requirement]:
        cfg = self.rules.cost_items
        if not cfg.get("enabled"):
            return []
        label = cfg.get("label") or "費用項目"
        base = Requirement(
            "cost:items", label, "ok", "", " ".join(str(cfg.get("ask", "")).split()),
            bool(cfg.get("waivable")), "cost",
        )
        if not self.cost_items:
            base.status, base.issue = "missing", "未入力"
        reqs = [base]
        if not self.cost_items or base.id in self.exemptions:
            return reqs  # line checks wait for lines; a waived list skips them

        groups_ja = cfg.get("cost_groups") or {}
        if cfg.get("require_amount"):
            for item in self.cost_items:
                if item["amount"] is None:
                    reqs.append(Requirement(
                        f"cost:amount:{item['label']}", f"「{item['label']}」の金額", "missing",
                        "金額が未入力", f"「{item['label']}」の金額はいくらですか？", False, "cost",
                    ))
        present = {item["cost_group"] for item in self.cost_items}
        vague_labels: set[str] = set()  # a vague line isn't also flagged as a lump sum
        for check in cfg.get("checks") or []:
            if check.get("when") is not None and not condition_met(check["when"], self.values):
                continue
            ctype, cid = check.get("type"), check.get("id")
            waivable = bool(check.get("waivable"))
            if ctype == "require_group":
                group = check["group"]
                if group not in present:
                    msg = _fmt(check.get("message", ""), group=groups_ja.get(group, group))
                    reqs.append(Requirement(
                        f"cost:{cid}", f"{groups_ja.get(group, group)}の項目", "missing", msg, msg, waivable, "cost",
                    ))
                continue
            for item in self.cost_items:
                item_label = item["label"]
                hit = False
                if ctype == "vague_label":
                    core = re.sub(r"[\s　]", "", item_label).replace("一式", "")
                    hit = core == "" or core in (check.get("generic_words") or [])
                    if hit:
                        vague_labels.add(item_label)
                elif ctype == "lump_sum":
                    hit = (
                        (check.get("marker") or "一式") in item_label
                        and not item["note"]
                        and item_label not in vague_labels
                    )
                elif ctype == "price_over":
                    threshold = self._threshold(check, item["cost_group"])
                    amount = item["amount"]
                    hit = amount is not None and threshold is not None and amount > threshold and not item["note"]
                if hit:
                    threshold = self._threshold(check, item["cost_group"])
                    msg = _fmt(
                        check.get("message", ""), label=item_label,
                        amount=_num_text(item["amount"]), threshold=_num_text(threshold),
                    )
                    reqs.append(Requirement(
                        f"cost:{cid}:{item_label}", f"「{item_label}」", "needs_detail", msg, msg, waivable, "cost",
                    ))
        return reqs

    @staticmethod
    def _threshold(check: dict[str, Any], group: str) -> float | None:
        thresholds = check.get("thresholds") or {}
        value = thresholds.get(group, thresholds.get("default"))
        return float(value) if value is not None else None

    def requirements(self, apply_exemptions: bool = True) -> list[Requirement]:
        reqs = [
            Requirement(f"field:{rule.key}", rule.label, *self._field_status(rule), rule.ask, rule.waivable, "field")
            for rule in self.fields
            if self.applies(rule)
        ]
        reqs += self._cost_requirements()
        if apply_exemptions:
            for req in reqs:
                if req.status in OPEN and req.waivable and req.id in self.exemptions:
                    req.status = "waived"
        return reqs

    def checklist(self) -> dict[str, Any]:
        reqs = self.requirements()
        remaining = [r for r in reqs if r.status in OPEN]
        return {
            "status": "ready" if not remaining else "incomplete",
            "remaining": [
                {"id": r.id, "label": r.label, "issue": r.issue, "ask": r.ask, "waivable": r.waivable}
                for r in remaining
            ],
            "waived": [{"id": r.id, "label": r.label, "reason": self.exemptions[r.id]["reason"]}
                       for r in reqs if r.status == "waived"],
            "done": sum(1 for r in reqs if r.status == "ok"),
            "total": len(reqs),
        }

    # ------------------------------------------------------------ views

    def display_value(self, rule: FieldRule, value: Any) -> str:
        if rule.type == "boolean":
            return "あり" if value else "なし"
        if rule.type == "list":
            cols = list(rule.item_fields)
            lines = []
            for row in value:
                name = row.get(cols[0], "")
                rest = [f"{rule.item_fields[c]}:{row[c]}" for c in cols[1:] if row.get(c)]
                lines.append(name + (f"（{'、'.join(rest)}）" if rest else ""))
            return "／".join(lines)
        return str(value)

    def cost_total(self) -> float:
        return sum(i["amount"] for i in self.cost_items if i["amount"] is not None)

    def state(self) -> dict[str, Any]:
        """Everything the worker screen shows, refreshed after each tool call."""
        reqs = {r.id: r for r in self.requirements()}
        fields = []
        for rule in self.fields:
            req = reqs.get(f"field:{rule.key}")
            value = self.values.get(rule.key)
            if req is None and _is_empty(value):
                continue  # conditional and not triggered
            fields.append({
                "key": rule.key,
                "label": rule.label,
                "source": rule.source,
                "value": None if _is_empty(value) else self.display_value(rule, value),
                "status": req.status if req else "ok",
                "issue": req.issue if req else "",
                "waivable": rule.waivable,
                "exemption": self.exemptions.get(f"field:{rule.key}"),
            })
        cost_reqs = [asdict(r) for r in reqs.values() if r.kind == "cost"]
        for r in cost_reqs:
            r["exemption"] = self.exemptions.get(r["id"])
        return {
            "job_id": self.job["job_id"],
            "fields": fields,
            "cost_items": self.cost_items,
            "cost_total": self.cost_total(),
            "cost_requirements": cost_reqs,
            "checklist": self.checklist(),
            "finalized": self.finalized_report is not None,
            "report_id": (self.finalized_report or {}).get("report_id"),
        }

    # ------------------------------------------------------------ output

    def qnavi_fields(self) -> dict[str, str]:
        """report_symptom / report_reason / report_content, as QNavi stores them."""
        out: dict[str, list[str]] = {"report_symptom": [], "report_reason": [], "report_content": []}
        for rule in self.fields:
            if rule.maps_to not in out:
                continue
            value = self.values.get(rule.key)
            exemption = self.exemptions.get(f"field:{rule.key}")
            if _is_empty(value) and not exemption:
                continue
            text = (
                self.display_value(rule, value) if not _is_empty(value)
                else f"未取得（理由：{exemption['reason']}）"
            )
            bucket = out[rule.maps_to]
            bucket.append(text if not bucket else f"【{rule.label}】{text}")
        return {k: "\n".join(v) for k, v in out.items()}

    def approver_flags(self) -> list[dict[str, str]]:
        flags = []
        for ex in self.exemptions.values():
            text = f"{ex['label']}：未取得（理由：{ex['reason']}）"
            if not ex["reason_is_specific"]:
                flags.append({"level": "alert", "text": text + " ※理由が具体的ではありません"})
            else:
                flags.append({"level": "warn", "text": text})
        for rule in self.fields:
            if rule.approver_note and self.applies(rule):
                flags.append({"level": "info", "text": rule.approver_note})
        checks = {c.get("type"): c for c in self.rules.cost_items.get("checks") or []}
        for item in self.cost_items:
            if not item["note"]:
                continue
            threshold = self._threshold(checks["price_over"], item["cost_group"]) if "price_over" in checks else None
            if item["amount"] is not None and threshold is not None and item["amount"] > threshold:
                flags.append({"level": "info", "text": (
                    f"「{item['label']}」{_num_text(item['amount'])}円は基準額{_num_text(threshold)}円超。"
                    f"作業者の説明：{item['note']}"
                )})
            elif "一式" in item["label"]:
                flags.append({"level": "info", "text": f"「{item['label']}」は一式計上。作業者の説明：{item['note']}"})
        return flags

    def approve_helper_payload(self) -> dict[str, Any]:
        """Body for approve-helper's POST /api/v2/estimation-evaluation
        (`price` is the line total there, as here)."""
        return {
            "large": self.job["large"],
            "description": self.job["description"],
            "items": [
                {"item_label": i["label"], "price": i["amount"], "quantity": i["quantity"]}
                for i in self.cost_items
                if i["amount"] is not None
            ],
        }

    def to_report(self, report_id: str, meta: dict[str, Any]) -> dict[str, Any]:
        checklist = self.checklist()
        return {
            "report_id": report_id,
            "status": "submitted",
            "completeness": "complete_with_exemptions" if self.exemptions else "complete",
            "submitted_at": now_iso(),
            "rules_version": self.rules.version,
            "job": self.job,
            "qnavi_fields": self.qnavi_fields(),
            "fields": [
                {
                    "key": rule.key,
                    "label": rule.label,
                    "source": rule.source,
                    "value": self.values.get(rule.key),
                    "display": self.display_value(rule, self.values[rule.key]) if rule.key in self.values else None,
                }
                for rule in self.fields
                if rule.key in self.values
            ],
            "cost_items": [
                {**i, "cost_group_ja": (self.rules.cost_items.get("cost_groups") or {}).get(i["cost_group"])}
                for i in self.cost_items
            ],
            "cost_total": self.cost_total(),
            "exemptions": list(self.exemptions.values()),
            "approver_flags": self.approver_flags(),
            "checklist": {"done": checklist["done"], "total": checklist["total"]},
            "approve_helper_payload": self.approve_helper_payload(),
            "transcript": self.transcript,
            "conversation": meta,
        }

    # ------------------------------------------------------------ drafts

    def to_draft(self) -> dict[str, Any]:
        return {
            "job_id": self.job["job_id"],
            "saved_at": now_iso(),
            "rules_version": self.rules.version,
            "values": self.values,
            "cost_items": self.cost_items,
            "exemptions": self.exemptions,
            "transcript": self.transcript,
            "checklist": self.checklist(),
        }

    def load_draft(self, draft: dict[str, Any]) -> None:
        """Re-apply a dropped call's answers through the same validation, so a
        draft written under older rules can't smuggle in invalid values."""
        self.set_values(draft.get("values") or {})
        self.set_cost_items(draft.get("cost_items") or [])
        for rid, ex in (draft.get("exemptions") or {}).items():
            self.exempt(rid, ex.get("reason", ""), ex.get("reason_is_specific", True))
        self.transcript = list(draft.get("transcript") or [])
        self.resumed = True
