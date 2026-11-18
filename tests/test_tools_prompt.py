from google.genai import types

from app.services.prompt import build_system_instruction
from app.services.tools import ToolHandler, build_declarations
from tests.conftest import fill_aircon


def handler(draft):
    saved = []
    h = ToolHandler(draft, on_finalize=lambda d: saved.append(1) or {"report_id": "R-TEST"})
    return h, saved


def test_declarations_cover_this_jobs_fields(draft):
    decls = {d.name: d for d in build_declarations(draft, types.Behavior.BLOCKING)}
    assert set(decls) == {"update_report", "set_cost_items", "record_exemption", "get_checklist", "finalize_report"}
    props = decls["update_report"].parameters.properties
    assert "refrigerant" in props and "glass_spec" not in props
    assert props["result"].enum == ["完了", "応急処置", "未完了"]
    assert decls["finalize_report"].behavior == types.Behavior.BLOCKING
    # The whole config must serialize, or the Live setup message fails.
    types.LiveConnectConfig(tools=[types.Tool(function_declarations=list(decls.values()))]).model_dump_json()


def test_every_result_carries_remaining_and_hint(draft):
    h, _ = handler(draft)
    result = h("update_report", {"symptom": "室外機のファンが回っていない"})
    assert result["checklist_status"] == "incomplete"
    assert result["remaining"][0]["id"] == "field:cause"
    assert "原因" in result["next_hint"]


def test_finalize_refused_until_ready_and_confirmed(draft):
    h, saved = handler(draft)
    assert not h("finalize_report", {"worker_confirmed": True})["ok"]
    fill_aircon(draft)
    assert not h("finalize_report", {"worker_confirmed": False})["ok"]
    result = h("finalize_report", {"worker_confirmed": True})
    assert result["ok"] and result["report_id"] == "R-TEST" and saved == [1]
    # Locked after finalize; finalize again is idempotent.
    assert not h("update_report", {"symptom": "変更"})["ok"]
    assert h("finalize_report", {"worker_confirmed": True})["ok"] and saved == [1]


def test_unknown_tool(draft):
    h, _ = handler(draft)
    assert not h("delete_everything", {})["ok"]


def test_prompt_contains_job_rules_and_no_internal_notes(draft):
    text = build_system_instruction(draft, "ja")
    assert "サンプル食堂 新宿南口店" in text
    assert "refrigerant「冷媒（ガス）補充の有無と量」" in text
    assert "条件付き必須（作業結果が完了のとき）" in text
    assert "Hiện trạng" not in text  # `vi:` notes stay out of the prompt
    assert "{label}" not in text and "{worker_name}" not in text


def test_prompt_vietnamese_demo_records_in_japanese(draft):
    text = build_system_instruction(draft, "vi")
    assert "ベトナム語で話してください" in text and "日本語に翻訳して記録" in text


def test_prompt_resume_lists_recorded_values(draft, rules, aircon_job):
    from app.services.report_draft import ReportDraft

    draft.set_values({"symptom": "室外機のファンが回っていない"})
    again = ReportDraft(aircon_job, rules)
    again.load_draft(draft.to_draft())
    text = build_system_instruction(again, "ja")
    assert "前回の通話の続き" in text and "室外機のファンが回っていない" in text
