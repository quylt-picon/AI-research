from tests.conftest import fill_aircon


def remaining_ids(draft):
    return [r["id"] for r in draft.checklist()["remaining"]]


def test_empty_draft_lists_required_fields_in_rule_order(draft):
    ids = remaining_ids(draft)
    assert ids[:3] == ["field:symptom", "field:cause", "field:cause_type"]
    assert "field:incident_detail" not in ids  # conditional, not triggered
    assert ids[-1] == "cost:items"


def test_complete_report_is_ready(draft):
    fill_aircon(draft)
    assert draft.checklist()["status"] == "ready", remaining_ids(draft)


def test_vague_text_needs_detail(draft):
    draft.set_values({"cause": "故障"})
    req = next(r for r in draft.checklist()["remaining"] if r["id"] == "field:cause")
    assert "具体性" in req["issue"]


def test_condition_adds_and_removes_fields(draft):
    draft.set_values({"result": "未完了"})
    assert "field:follow_up" in remaining_ids(draft)
    assert "field:operation_check" not in remaining_ids(draft)
    draft.set_values({"result": "完了"})
    assert "field:follow_up" not in remaining_ids(draft)
    assert "field:operation_check" in remaining_ids(draft)


def test_accident_cause_asks_for_detail_and_flags_insurance(draft):
    draft.set_values({"cause_type": "人為的・事故"})
    assert "field:incident_detail" in remaining_ids(draft)
    assert any("保険" in f["text"] for f in draft.approver_flags())


def test_enum_accepts_close_match_and_rejects_unknown(draft):
    saved, rejected = draft.set_values({"result": "応急"})
    assert saved == ["result"] and draft.values["result"] == "応急処置"
    saved, rejected = draft.set_values({"result": "たぶん大丈夫"})
    assert rejected and rejected[0]["key"] == "result"


def test_unknown_field_is_rejected(draft):
    _, rejected = draft.set_values({"glass_spec": "5mm"})  # a ガラス field, not aircon
    assert rejected[0]["key"] == "glass_spec"


def test_expect_true_needs_reason_when_false(draft):
    draft.set_values({"photos_taken": False})
    assert "field:photos_taken" in remaining_ids(draft)
    assert draft.exempt("field:photos_taken", "店舗が撮影禁止")["ok"]
    assert "field:photos_taken" not in remaining_ids(draft)


def test_non_waivable_cannot_be_exempted(draft):
    result = draft.exempt("field:work_content", "時間がない")
    assert not result["ok"] and "免除できません" in result["error"]


def test_exemption_dropped_once_value_given(draft):
    draft.exempt("field:error_code", "リモコンが見当たらない")
    assert "field:error_code" in draft.exemptions
    draft.set_values({"error_code": "E7"})
    assert "field:error_code" not in draft.exemptions


def test_insufficient_reason_is_flagged_for_approver(draft):
    draft.exempt("field:unit_maker_model", "忘れた", reason_is_specific=False)
    alert = [f for f in draft.approver_flags() if f["level"] == "alert"]
    assert alert and "具体的ではありません" in alert[0]["text"]


def test_cost_checks_mirror_approve_helper_cases(draft):
    fill_aircon(draft)
    draft.set_cost_items([
        {"label": "修理一式", "cost_group": "labor", "amount": 45000},
        {"label": "出張費", "cost_group": "support", "amount": 5000},
    ])
    ids = remaining_ids(draft)
    assert "cost:material_missing" in ids          # III: parts used, no 材料費
    assert "cost:vague_label:修理一式" in ids       # IV: name says nothing
    assert "cost:lump_sum:修理一式" not in ids      # not double-flagged
    assert "cost:price_reason:修理一式" in ids      # VIII-like: over 30,000 labor


def test_lump_sum_resolved_by_note(draft):
    fill_aircon(draft)
    draft.set_cost_items([
        {"label": "ファンモーター", "cost_group": "material", "amount": 32000},
        {"label": "配管修理一式", "cost_group": "labor", "amount": 20000},
    ])
    assert "cost:lump_sum:配管修理一式" in remaining_ids(draft)
    draft.set_cost_items([
        {"label": "ファンモーター", "cost_group": "material", "amount": 32000},
        {"label": "配管修理一式", "cost_group": "labor", "amount": 20000, "note": "作業費のみ。材料は別行"},
    ])
    assert draft.checklist()["status"] == "ready"


def test_price_over_threshold_not_waivable_until_note(draft):
    fill_aircon(draft)
    draft.set_cost_items([
        {"label": "ファンモーター", "cost_group": "material", "amount": 60000},
        {"label": "交換作業", "cost_group": "labor", "amount": 18000},
    ])
    assert "cost:price_reason:ファンモーター" in remaining_ids(draft)
    assert not draft.exempt("cost:price_reason:ファンモーター", "高い")["ok"]


def test_waived_cost_list_skips_line_checks(draft):
    fill_aircon(draft)
    draft.set_cost_items([])
    assert draft.exempt("cost:items", "見積は別途書面で提出")["ok"]
    assert draft.checklist()["status"] == "ready"


def test_amounts_stay_integers(draft):
    draft.set_cost_items([{"label": "出張費", "cost_group": "support", "amount": "5,000円"}])
    assert draft.cost_items[0]["amount"] == 5000 and isinstance(draft.cost_items[0]["amount"], int)


def test_qnavi_fields_and_payload(draft):
    fill_aircon(draft)
    q = draft.qnavi_fields()
    assert q["report_symptom"].startswith("室外機")
    assert q["report_content"].startswith("室外機のファンモーターを交換した")
    assert "【使用部材" in q["report_content"]
    payload = draft.approve_helper_payload()
    assert payload["large"] == "エアコン"
    assert payload["items"][0] == {"item_label": "ファンモーター", "price": 32000, "quantity": 1}


def test_waived_mapped_field_shows_reason_in_qnavi_column(draft):
    draft.exempt("field:cause", "メーカー調査待ち")
    assert draft.qnavi_fields()["report_reason"] == "未取得（理由：メーカー調査待ち）"


def test_draft_round_trip(draft, rules, aircon_job):
    from app.services.report_draft import ReportDraft

    draft.set_values({"symptom": "室外機のファンが回っていない"})
    draft.exempt("field:error_code", "リモコンが見当たらない")
    again = ReportDraft(aircon_job, rules)
    again.load_draft(draft.to_draft())
    assert again.values == draft.values
    assert set(again.exemptions) == {"field:error_code"}
    assert again.resumed
