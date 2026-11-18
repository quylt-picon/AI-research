import pytest

from app.services.rules import RulesError, condition_met, load_rules


def keys(fields):
    return [f.key for f in fields]


def test_rules_file_is_valid(rules):
    assert rules.version
    assert rules.cost_items["enabled"]


def test_common_fields_apply_to_unknown_category(rules):
    ks = keys(rules.fields_for("見積", "概算見積り依頼"))
    assert ks[:3] == ["symptom", "cause", "cause_type"]
    assert "error_code" not in ks


def test_category_and_symptom_layers(rules):
    ks = keys(rules.fields_for("エアコン", "冷えない"))
    assert {"unit_maker_model", "error_code", "refrigerant"} <= set(ks)
    assert "drain_status" not in ks
    assert "drain_status" in keys(rules.fields_for("エアコン", "水漏れ"))


def test_symptom_match_is_substring(rules):
    # raw QNavi labels like "つまり：シンク・その他" still hit the 詰まり rules
    assert "clog_cause" in keys(rules.fields_for("水回り", "つまり：シンク・その他"))
    assert "leak_point" in keys(rules.fields_for("水回り", "水漏れ：配管"))


def test_mapped_fields_point_at_qnavi_columns(rules):
    mapped = {f.maps_to for f in rules.fields_for("エアコン", "冷えない") if f.maps_to}
    assert mapped == {"report_symptom", "report_reason", "report_content"}


@pytest.mark.parametrize(
    "cond, values, expected",
    [
        ({"field": "result", "equals": "完了"}, {"result": "完了"}, True),
        ({"field": "result", "equals": "完了"}, {}, False),
        ({"field": "result", "in": ["応急処置", "未完了"]}, {"result": "未完了"}, True),
        ({"field": "result", "not_in": ["完了"]}, {}, False),
        ({"field": "result", "not_equals": "完了"}, {"result": "応急処置"}, True),
    ],
)
def test_condition_met(cond, values, expected):
    assert condition_met(cond, values) is expected


def test_bad_rules_are_rejected(tmp_path):
    bad = tmp_path / "r.yaml"
    bad.write_text("common_fields:\n  - key: a\n    label: A\n    type: enum\n", encoding="utf-8")
    with pytest.raises(RulesError, match="options"):
        load_rules(bad)


def test_bad_condition_is_rejected(tmp_path):
    bad = tmp_path / "r.yaml"
    bad.write_text(
        "common_fields:\n  - key: a\n    label: A\n    required_when: {field: b}\n", encoding="utf-8"
    )
    with pytest.raises(RulesError, match="toán tử"):
        load_rules(bad)
