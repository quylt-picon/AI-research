"""Function declarations Gemini can call, and the handlers behind them.

Every handler returns the checklist's `remaining` and a `next_hint`, so the
model always asks from the code's list rather than from memory.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable

from google.genai import types

from app.services.report_draft import COST_GROUP_KEYS, ReportDraft
from app.services.rules import FieldRule


def _field_schema(rule: FieldRule) -> dict[str, Any]:
    desc = rule.label + (f"。{' '.join(rule.guide.split())}" if rule.guide else "")
    if rule.type == "enum":
        return {"type": "STRING", "enum": list(rule.options), "description": desc}
    if rule.type == "boolean":
        return {"type": "BOOLEAN", "description": desc}
    if rule.type == "list":
        return {
            "type": "ARRAY",
            "description": desc,
            "items": {
                "type": "OBJECT",
                "properties": {k: {"type": "STRING", "description": v} for k, v in rule.item_fields.items()},
            },
        }
    return {"type": "STRING", "description": desc}


def build_declarations(draft: ReportDraft, behavior: types.Behavior | None) -> list[types.FunctionDeclaration]:
    def decl(name: str, description: str, parameters: dict[str, Any] | None = None) -> types.FunctionDeclaration:
        kw: dict[str, Any] = {"name": name, "description": description}
        if parameters:
            kw["parameters"] = parameters
        if behavior is not None:
            kw["behavior"] = behavior
        return types.FunctionDeclaration(**kw)

    groups = draft.rules.cost_items.get("cost_groups") or {}
    return [
        decl(
            "update_report",
            "報告項目を記録・訂正する。作業者が話した項目だけを渡す（複数可）。値を消すときは空文字。",
            {"type": "OBJECT", "properties": {r.key: _field_schema(r) for r in draft.fields}},
        ),
        decl(
            "set_cost_items",
            "費用項目（見積・請求の内訳）を全行まとめて記録する。送った内容で全体が置き換わる。",
            {
                "type": "OBJECT",
                "properties": {
                    "items": {
                        "type": "ARRAY",
                        "items": {
                            "type": "OBJECT",
                            "properties": {
                                "label": {"type": "STRING", "description": "項目名（何の部材・作業かわかる名称）"},
                                "cost_group": {
                                    "type": "STRING",
                                    "enum": list(COST_GROUP_KEYS),
                                    "description": " / ".join(f"{k}={v}" for k, v in groups.items()),
                                },
                                "quantity": {"type": "NUMBER", "description": "数量"},
                                "unit": {"type": "STRING", "description": "単位（個・m・式・時間など）"},
                                "amount": {"type": "NUMBER", "description": "その行の合計金額（税抜・円）。値引きはマイナス"},
                                "note": {"type": "STRING", "description": "内訳・金額の根拠などのメモ"},
                            },
                            "required": ["label", "cost_group"],
                        },
                    }
                },
                "required": ["items"],
            },
        ),
        decl(
            "record_exemption",
            "作業者が提供できない項目について、その理由を記録する（免除可の項目のみ）。",
            {
                "type": "OBJECT",
                "properties": {
                    "requirement_id": {"type": "STRING", "description": "remaining に出ている id"},
                    "reason": {"type": "STRING", "description": "作業者が話した理由（日本語）"},
                    "reason_is_specific": {
                        "type": "BOOLEAN",
                        "description": "理由が具体的なら true。「忘れた」「わからない」だけなら false",
                    },
                },
                "required": ["requirement_id", "reason", "reason_is_specific"],
            },
        ),
        decl("get_checklist", "現在の未充足項目（remaining）を確認する。"),
        decl(
            "finalize_report",
            "報告を確定して承認者に送る。全項目が揃い、読み上げ内容を作業者が確認した後にだけ呼ぶ。",
            {
                "type": "OBJECT",
                "properties": {
                    "worker_confirmed": {
                        "type": "BOOLEAN",
                        "description": "読み上げた内容を作業者が「はい」と確認したら true",
                    }
                },
                "required": ["worker_confirmed"],
            },
        ),
    ]


def _progress(draft: ReportDraft) -> dict[str, Any]:
    checklist = draft.checklist()
    if draft.finalized_report is not None:
        hint = "報告は確定済みです。受付完了を伝えて終話してください。"
    elif checklist["status"] == "ready":
        hint = "すべての項目が揃いました。報告内容を短く読み上げ、送ってよいか作業者に確認してください。"
    else:
        first = checklist["remaining"][0]
        hint = f"次は「{first['label']}」を聞いてください。{first['issue'] if first['issue'] != '未入力' else ''}一度に1つずつ。"
    return {
        "checklist_status": checklist["status"],
        "remaining": [{k: r[k] for k in ("id", "label", "issue", "waivable")} for r in checklist["remaining"]],
        "next_hint": hint.strip(),
    }


class ToolHandler:
    """Runs one tool call against the draft. `on_finalize(draft)` saves the
    report and returns it; it is injected so tests need no file system."""

    def __init__(self, draft: ReportDraft, on_finalize: Callable[[ReportDraft], dict[str, Any]]):
        self.draft = draft
        self.on_finalize = on_finalize

    def __call__(self, name: str, args: dict[str, Any] | None) -> dict[str, Any]:
        args = dict(args or {})
        handler = getattr(self, f"_{name}", None)
        if handler is None:
            result = {"ok": False, "error": f"unknown tool {name}"}
        else:
            result = handler(args)
        self.draft.tool_log.append({
            "at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "name": name,
            "args": args,
            "ok": result.get("ok", True),
        })
        return result

    def _update_report(self, args: dict[str, Any]) -> dict[str, Any]:
        if self.draft.finalized_report is not None:
            return {"ok": False, "error": "報告は確定済みです。", **_progress(self.draft)}
        saved, rejected = self.draft.set_values(args)
        return {"ok": not rejected, "saved": saved, "rejected": rejected, **_progress(self.draft)}

    def _set_cost_items(self, args: dict[str, Any]) -> dict[str, Any]:
        if self.draft.finalized_report is not None:
            return {"ok": False, "error": "報告は確定済みです。", **_progress(self.draft)}
        rejected = self.draft.set_cost_items(args.get("items") or [])
        return {
            "ok": not rejected,
            "rejected": rejected,
            "recorded": [f"{i['label']} {i['amount']}円" for i in self.draft.cost_items],
            "total": self.draft.cost_total(),
            **_progress(self.draft),
        }

    def _record_exemption(self, args: dict[str, Any]) -> dict[str, Any]:
        result = self.draft.exempt(
            str(args.get("requirement_id") or ""),
            str(args.get("reason") or ""),
            bool(args.get("reason_is_specific", True)),
        )
        return {**result, **_progress(self.draft)}

    def _get_checklist(self, args: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, **_progress(self.draft)}

    def _finalize_report(self, args: dict[str, Any]) -> dict[str, Any]:
        if self.draft.finalized_report is not None:
            return {"ok": True, "report_id": self.draft.finalized_report["report_id"], **_progress(self.draft)}
        checklist = self.draft.checklist()
        if checklist["status"] != "ready":
            return {"ok": False, "error": "未充足の項目があります。確定できません。", **_progress(self.draft)}
        if self.draft.rules.finalize.get("require_confirmation", True) and not args.get("worker_confirmed"):
            return {
                "ok": False,
                "error": "作業者の確認が取れていません。内容を読み上げて確認してから呼んでください。",
                **_progress(self.draft),
            }
        report = self.on_finalize(self.draft)
        self.draft.finalized_report = report
        return {"ok": True, "report_id": report["report_id"], **_progress(self.draft)}
