"""Render the rules + the job into the system instruction Gemini Live reads.

Only what the model needs goes in: YAML comments and `vi:` notes stay
behind. Field keys are kept next to labels because the model has to use the
keys in tool calls.
"""

from __future__ import annotations

from typing import Any

from app.services.report_draft import ReportDraft

LANGUAGES = {
    "ja": "作業者は日本語で話します。作業者とは日本語で話してください。",
    "vi": (
        "これは社内デモです。作業者はベトナム語で話します（日本語の専門用語が混ざることがあります）。"
        "作業者の音声はベトナム語として聞き取ってください。"
        "作業者とはベトナム語で話してください（挨拶・質問・読み上げもベトナム語）。"
        "ただし、ツールに渡す値（報告書に記録する内容）は必ず自然な日本語に翻訳して記録してください。"
        "承認者は日本人です。"
    ),
}


def _one_line(text: Any) -> str:
    """Folded YAML joins lines with a space; Japanese needs none after 。"""
    return " ".join(str(text or "").split()).replace("。 ", "。")


def _value_text(value: Any) -> str:
    if isinstance(value, bool):
        return "あり" if value else "なし"
    if isinstance(value, (list, tuple)):
        return "・".join(str(v) for v in value)
    return str(value)


def _requirement_tag(draft: ReportDraft, rule) -> str:
    if rule.required:
        need = "必須"
    elif rule.required_when:
        cond = rule.required_when
        op = next(k for k in ("equals", "not_equals", "in", "not_in") if k in cond)
        target = draft._by_key.get(cond["field"])
        name = target.label if target else cond["field"]
        value = _value_text(cond[op])
        phrase = {
            "equals": f"{name}が{value}",
            "in": f"{name}が{value}のいずれか",
            "not_equals": f"{name}が{value}以外",
            "not_in": f"{name}が{value}以外",
        }[op]
        need = f"条件付き必須（{phrase}のとき）"
    else:
        need = "任意"
    waive = "免除可" if rule.waivable else "免除不可"
    return f"{need}・{waive}"


def _field_block(draft: ReportDraft, rule) -> str:
    lines = [f"- {rule.key}「{rule.label}」［{_requirement_tag(draft, rule)}］"]
    if rule.type == "enum":
        lines.append(f"  選択肢: {' / '.join(rule.options)}")
    elif rule.type == "boolean":
        lines.append("  値: true / false")
    elif rule.type == "list":
        cols = "、".join(f"{k}={v}" for k, v in rule.item_fields.items())
        lines.append(f"  値: 行のリスト（{cols}）")
    if rule.expect is not None:
        lines.append(f"  充足条件: 値が {str(rule.expect).lower()} であること。そうでなければ理由を聞いて免除として記録")
    if rule.ask:
        lines.append(f"  質問例: {_one_line(rule.ask)}")
    if rule.guide:
        lines.append(f"  基準: {_one_line(rule.guide)}")
    if rule.example:
        lines.append(f"  記録例: {_one_line(rule.example)}")
    return "\n".join(lines)


def _cost_block(draft: ReportDraft) -> str:
    cfg = draft.rules.cost_items
    if not cfg.get("enabled"):
        return "費用項目は聞かなくてよい。"
    groups = cfg.get("cost_groups") or {}
    lines = [
        f"「{cfg.get('label', '費用項目')}」は set_cost_items で記録する。呼ぶたびに全行を送る（送った内容で置き換わる）。",
        "各行: label=項目名, cost_group=" + " / ".join(f"{k}（{v}）" for k, v in groups.items())
        + ", quantity=数量, unit=単位, amount=その行の合計金額（税抜・円）, note=内訳・根拠のメモ",
        f"質問例: {_one_line(cfg.get('ask'))}",
        f"必須: {'はい' if cfg.get('required') else 'いいえ'}（{'免除可' if cfg.get('waivable') else '免除不可'}）",
        "チェック（ツールの結果で指摘されたら対応する）:",
    ]
    for check in cfg.get("checks") or []:
        msg = (
            _one_line(check.get("message"))
            .replace("{label}", "〇〇").replace("{amount}円", "金額").replace("{threshold}円", "下記")
        )
        if check.get("type") == "price_over":
            th = check.get("thresholds") or {}
            msg += "（基準額: " + "、".join(
                f"{groups.get(k, 'その他') if k != 'default' else 'その他'} {int(v):,}円" for k, v in th.items()
            ) + "）"
        lines.append(f"- {msg}")
    return "\n".join(lines)


def _resume_block(draft: ReportDraft) -> str:
    if not draft.resumed:
        return ""
    recorded = [
        f"- {rule.key}「{rule.label}」: {draft.display_value(rule, draft.values[rule.key])}"
        for rule in draft.fields
        if rule.key in draft.values
    ]
    for item in draft.cost_items:
        recorded.append(f"- 費用: {item['label']} {item['amount']}円（{item['cost_group']}）")
    for ex in draft.exemptions.values():
        recorded.append(f"- 免除: {ex['label']}（理由: {ex['reason']}）")
    body = "\n".join(recorded) or "（なし）"
    return (
        "\n# 前回の通話の続き\n"
        "前回の通話が途中で切れました。以下は記録済みなので聞き直さないこと。"
        "挨拶では「前回の続きから伺います」と伝え、残りの項目だけを聞く。\n"
        f"{body}\n"
    )


def build_system_instruction(draft: ReportDraft, lang: str = "ja") -> str:
    rules, job = draft.rules, draft.job
    conv = rules.conversation
    policy = rules.exemption_policy
    issue = f"{job['large']}／{job['small']}"
    opening = _one_line(conv.get("opening")).format(
        worker_name=job["worker"]["name"], store_name=job["store_name"], issue=issue
    )

    groups: dict[str, list[str]] = {}
    for rule in draft.fields:
        groups.setdefault(rule.source, []).append(_field_block(draft, rule))
    field_text = "\n\n".join(f"## {src}\n" + "\n".join(blocks) for src, blocks in groups.items())

    principles = "\n".join(f"- {_one_line(p)}" for p in conv.get("principles") or [])
    good = "、".join(policy.get("specific_reason_examples") or [])
    bad = "、".join(policy.get("insufficient_reason_examples") or [])

    return f"""# 役割
{_one_line(conv.get('persona'))}

# 今回の案件（システム登録済み。作業者に聞き直さないこと）
- 案件番号: {job['trouble_number']}
- 店舗: {job['store_name']}
- 区分: {issue}
- 店舗からの依頼内容: {_one_line(job['description'])}
- 作業者: {job['worker']['name']}さん
- 訪問日: {job['visit_date']}

# 言語
{LANGUAGES.get(lang, LANGUAGES['ja'])}

# 話し方のルール
{principles}

# 進め方
1. 最初に次の趣旨で挨拶する: 「{opening}」
2. 作業者の話から取れる項目は、聞き終わったらすぐ update_report / set_cost_items で記録する（複数項目をまとめて記録してよい）。
3. ツールの結果の remaining（未充足の項目）を見て、上から順に1つずつ聞く。remaining が空になるまで続ける。自分の判断で項目を飛ばさない。
4. 作業者が答えられない項目は「免除のルール」に従う。
5. checklist_status が ready になったら、読み上げて確認する: {_one_line(rules.finalize.get('read_back'))}
6. 作業者が「はい」と確認したら finalize_report(worker_confirmed=true) を呼ぶ。訂正があれば記録し直してから再度読み上げる。
7. finalize_report が成功したら、次の趣旨で終話する: 「{_one_line(conv.get('closing'))}」

# 報告項目
{field_text}

# 費用項目
{_cost_block(draft)}

# 免除のルール
- {_one_line(policy.get('principle'))}
- 具体的な理由の例: {good}
- 不十分な理由の例: {bad}
- {_one_line(policy.get('on_insufficient'))}
- {_one_line(policy.get('not_waivable'))}
- 免除は record_exemption で記録する。requirement_id はツール結果の remaining の id をそのまま使う。

# ツールの使い方
- update_report: 報告項目を記録・訂正する。キーは上の項目キー。
- set_cost_items: 費用項目を全行まとめて記録する。
- record_exemption: 作業者が提供できない項目の理由を記録する。
- get_checklist: 現在の未充足項目を確認する。
- finalize_report: 読み上げ確認後に報告を確定する。ready でないと失敗する。
- どのツールも結果に remaining と next_hint が返る。必ず従う。
- ツールの呼び出しについて作業者に説明しない（「記録します」程度でよい）。
{_resume_block(draft)}"""
