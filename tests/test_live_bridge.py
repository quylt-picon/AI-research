"""Browser WebSocket -> bridge -> tools -> saved report, against a scripted
fake Gemini session (no network, no cost)."""

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from google.genai import types

import app.services.live_bridge as bridge
from app.core.config import settings
from app.main import app
from app.services import storage

AUDIO = b"\x00\x01" * 240


def _content(**kw):
    return types.LiveServerMessage(server_content=types.LiveServerContent(**kw))


def _say(text):
    return [
        _content(output_transcription=types.Transcription(text=text)),
        _content(model_turn=types.Content(parts=[types.Part(inline_data=types.Blob(data=AUDIO, mime_type="audio/pcm"))])),
    ]


def _call(name, args, cid):
    return types.LiveServerMessage(
        tool_call=types.LiveServerToolCall(function_calls=[types.FunctionCall(id=cid, name=name, args=args)])
    )


FIELDS = {
    "symptom": "室外機のファンが回っておらずE7表示",
    "cause": "ファンモーターの経年劣化による焼損",
    "cause_type": "経年劣化",
    "work_content": "室外機のファンモーターを交換した",
    "result": "完了",
    "operation_check": "試運転30分で冷風を確認",
    "parts_replaced": True,
    "parts_used": [{"name": "ファンモーター", "quantity": "1"}],
    "photos_taken": True,
    "store_confirmation": "店長に説明済み",
    "error_code": "E7",
    "refrigerant": "補充なし",
}
ITEMS = [
    {"label": "ファンモーター", "cost_group": "material", "amount": 32000},
    {"label": "ファンモーター交換作業", "cost_group": "labor", "amount": 18000},
]

SCRIPT = [
    [*_say("お疲れさまです。"), _content(turn_complete=True)],
    [
        _content(input_transcription=types.Transcription(text="ファンモーターを交換しました")),
        _call("update_report", FIELDS, "c1"),
        _call("set_cost_items", {"items": ITEMS}, "c2"),
        _call("record_exemption", {"requirement_id": "field:unit_maker_model",
                                   "reason": "銘板が汚れて読めない", "reason_is_specific": True}, "c3"),
        _call("finalize_report", {"worker_confirmed": True}, "c4"),
        *_say("報告を受け付けました。"),
        types.LiveServerMessage(usage_metadata=types.UsageMetadata(
            prompt_tokens_details=[types.ModalityTokenCount(modality=types.MediaModality.AUDIO, token_count=1000)],
            response_tokens_details=[types.ModalityTokenCount(modality=types.MediaModality.AUDIO, token_count=500)],
        )),
        _content(turn_complete=True),
    ],
]


class FakeSession:
    def __init__(self, script):
        self.script = list(script)
        self.sent = []
        self.tool_responses = []

    async def send_realtime_input(self, **kw):
        self.sent.append(kw)

    async def send_tool_response(self, function_responses):
        self.tool_responses.extend(function_responses)

    async def receive(self):
        if not self.script:
            return
        for msg in self.script.pop(0):
            yield msg


@pytest.fixture
def fake_gemini(monkeypatch):
    holder = {}

    @asynccontextmanager
    async def connect(model, config):
        holder["config"] = config
        holder["session"] = FakeSession(SCRIPT)
        yield holder["session"]

    client = SimpleNamespace(aio=SimpleNamespace(live=SimpleNamespace(connect=connect)))
    monkeypatch.setattr(bridge.genai, "Client", lambda **kw: client)
    monkeypatch.setattr(settings, "gemini_api_key", "test-key")
    return holder


def _drain(ws):
    events, audio = [], 0
    while True:
        msg = ws.receive()
        if msg.get("bytes"):
            audio += 1
            continue
        if msg.get("text"):
            event = json.loads(msg["text"])
            events.append(event)
            if event["type"] in ("ended", "error"):
                return events, audio


def test_full_call_saves_report(fake_gemini):
    with TestClient(app) as client, client.websocket_connect("/ws/report/J-1001?lang=ja") as ws:
        events, audio = _drain(ws)

    kinds = [e["type"] for e in events]
    assert "error" not in kinds, events
    assert {"transcript", "tool", "finalized", "ended"} <= set(kinds)
    assert audio == 2

    session = fake_gemini["session"]
    assert session.sent[0]["text"].startswith("（作業者が通話に参加しました")
    assert [r.id for r in session.tool_responses] == ["c1", "c2", "c3", "c4"]
    assert session.tool_responses[-1].response["ok"] is True

    report_id = next(e for e in events if e["type"] == "finalized")["report_id"]
    report = storage.get_report(report_id)
    assert report["completeness"] == "complete_with_exemptions"
    assert report["qnavi_fields"]["report_reason"] == "ファンモーターの経年劣化による焼損"
    assert [t["role"] for t in report["transcript"]] == ["agent", "worker", "agent"]
    assert report["transcript"][-1]["text"] == "報告を受け付けました。"  # closing words kept
    assert report["conversation"]["usage_tokens"]["audio_in"] == 1000
    assert report["conversation"]["estimated_cost_usd"] == pytest.approx((1000 * 3 + 500 * 12) / 1e6)
    assert storage.load_draft("J-1001") is None


def test_config_sent_to_gemini(fake_gemini):
    with TestClient(app) as client, client.websocket_connect("/ws/report/J-1001") as ws:
        _drain(ws)
    config = fake_gemini["config"]
    assert config.response_modalities == [types.Modality.AUDIO]
    assert "サンプル食堂" in config.system_instruction
    assert config.input_audio_transcription.language_codes == ["ja-JP"]
    assert "室外機" in config.input_audio_transcription.custom_vocabulary
    assert config.tools[0].function_declarations[0].name == "update_report"


def test_vietnamese_demo_hears_vietnamese(fake_gemini):
    """Regression: without a language hint, and with the Japanese trade
    vocabulary on, Vietnamese speech was transcribed as Japanese."""
    with TestClient(app) as client, client.websocket_connect("/ws/report/J-1001?lang=vi") as ws:
        _drain(ws)
    asr = fake_gemini["config"].input_audio_transcription
    assert asr.language_codes == ["vi-VN"]
    assert not asr.custom_vocabulary
    assert "ベトナム語として聞き取って" in fake_gemini["config"].system_instruction


def test_language_hint_refused_falls_back(monkeypatch):
    configs = []

    @asynccontextmanager
    async def connect(model, config):
        configs.append(config)
        if config.input_audio_transcription.language_codes:
            raise RuntimeError("1007 Invalid JSON payload: Unknown name language_codes")
        yield FakeSession([[*_say("こんにちは"), _content(turn_complete=True)]])

    client = SimpleNamespace(aio=SimpleNamespace(live=SimpleNamespace(connect=connect)))
    monkeypatch.setattr(bridge.genai, "Client", lambda **kw: client)
    monkeypatch.setattr(settings, "gemini_api_key", "test-key")
    with TestClient(app) as tc, tc.websocket_connect("/ws/report/J-1001?lang=vi") as ws:
        events, _ = _drain(ws)
    assert [bool(c.input_audio_transcription.language_codes) for c in configs] == [True, False]
    assert any(e.get("status") == "language_hint_unsupported" for e in events)
    assert any(e.get("status") == "connected" for e in events)


def test_dropped_call_keeps_a_draft(fake_gemini, monkeypatch):
    monkeypatch.setattr(bridge, "KICKOFF", "kick")
    script = [[_call("update_report", {"symptom": "室外機のファンが回っていない"}, "c1"), _content(turn_complete=True)]]

    @asynccontextmanager
    async def connect(model, config):
        yield FakeSession(script)

    client = SimpleNamespace(aio=SimpleNamespace(live=SimpleNamespace(connect=connect)))
    monkeypatch.setattr(bridge.genai, "Client", lambda **kw: client)
    with TestClient(app) as tc, tc.websocket_connect("/ws/report/J-1001") as ws:
        events, _ = _drain(ws)
    assert "draft_saved" in [e["type"] for e in events]
    assert storage.load_draft("J-1001")["values"]["symptom"] == "室外機のファンが回っていない"


def test_no_api_key_is_reported(monkeypatch):
    monkeypatch.setattr(settings, "gemini_api_key", "")
    with TestClient(app) as client, client.websocket_connect("/ws/report/J-1001") as ws:
        events, _ = _drain(ws)
    assert events[-1]["type"] == "error" and "GEMINI_API_KEY" in events[-1]["message"]


def test_rest_endpoints():
    with TestClient(app) as client:
        jobs = client.get("/api/jobs").json()
        assert len(jobs) == 5 and jobs[0]["status"] == "open"
        checklist = client.get("/api/jobs/J-1002/checklist").json()
        assert any(f["key"] == "clog_cause" for f in checklist["fields"])
        prompt = client.get("/api/jobs/J-1003/prompt").text
        assert "temporary_protection" in prompt
        assert client.get("/api/rules").json()["version"]
        assert client.post("/api/rules/reload").json()["ok"]
        assert client.get("/api/reports").json() == []
        assert client.get("/api/reports/..%2F..%2Fetc").status_code == 404
        cfg = client.get("/api/config").json()
        assert "AQ." not in json.dumps(cfg)  # key never sent to the browser
