"""One report call: browser WebSocket <-> Gemini Live session.

Browser -> server : binary = 16 kHz 16-bit mono PCM from the mic
                    text   = JSON {"type": "text", "text": ...} | {"type": "audio_end"} | {"type": "stop"}
Server -> browser : binary = 24 kHz 16-bit mono PCM to play
                    text   = JSON {"type": "transcript" | "state" | "tool" | "interrupted" |
                                   "turn_complete" | "status" | "finalized" | "usage" | "error" | "ended", ...}

The API key never leaves the server; tool calls run here against the draft.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from datetime import datetime
from typing import Any

from fastapi import WebSocket
from google import genai
from google.genai import types

from app.core.config import settings
from app.services import storage
from app.services.prompt import build_system_instruction
from app.services.report_draft import ReportDraft, now_iso
from app.services.rules import RuleSet
from app.services.tools import ToolHandler, build_declarations

logger = logging.getLogger("live")

KICKOFF = "（作業者が通話に参加しました。挨拶から始めてください。）"
TIME_WARNING = "（システム: 通話の残り時間が1分です。要点だけ確認し、終わらなければ途中保存して後でかけ直せることを伝えてください。）"
_FINALIZE_GRACE_SECONDS = 20

# What the worker speaks, per conversation language — told to the speech
# recognizer so it doesn't auto-detect (left alone, it heard Vietnamese as
# Japanese, pulled there by the Japanese context and vocabulary).
# `japanese_vocabulary`: bias towards the rules' Japanese trade terms —
# right for Japanese speech, harmful for Vietnamese.
SPEECH_INPUT = {
    "ja": {"language_codes": ["ja-JP"], "japanese_vocabulary": True},
    "vi": {"language_codes": ["vi-VN"], "japanese_vocabulary": False},
}


class LiveCall:
    def __init__(self, ws: WebSocket, job: dict[str, Any], rules: RuleSet, lang: str):
        self.ws = ws
        self.job = job
        self.lang = lang
        self.draft = ReportDraft(job, rules)
        prior = storage.load_draft(job["job_id"])
        if prior:
            self.draft.load_draft(prior)
        self.tools = ToolHandler(self.draft, on_finalize=self._save_final)
        self.session = None
        self.worker_buf = ""
        self.agent_buf = ""
        self.end_after_turn = False
        self.finalized_at: float | None = None
        self.spoke_after_final = False
        self.started = time.monotonic()
        self.started_iso = now_iso()
        self.usage = {"audio_in": 0, "text_in": 0, "audio_out": 0, "text_out": 0, "turns": 0}
        self.ws_open = True

    # ------------------------------------------------------------ helpers

    async def send_json(self, data: dict[str, Any]) -> None:
        if not self.ws_open:
            return
        try:
            await self.ws.send_text(json.dumps(data, ensure_ascii=False, default=str))
        except Exception:  # browser went away mid-send
            self.ws_open = False

    async def send_state(self) -> None:
        await self.send_json({"type": "state", "state": self.draft.state(), "resumed": self.draft.resumed})

    def _commit(self, role: str) -> None:
        buf = self.worker_buf if role == "worker" else self.agent_buf
        if buf.strip():
            self.draft.transcript.append({"role": role, "text": buf.strip(), "at": now_iso()})
        if role == "worker":
            self.worker_buf = ""
        else:
            self.agent_buf = ""

    def _flush(self) -> None:
        self._commit("worker")
        self._commit("agent")

    def cost_usd(self) -> float:
        u = self.usage
        return round(
            (u["audio_in"] * settings.price_audio_in + u["text_in"] * settings.price_text_in
             + u["audio_out"] * settings.price_audio_out + u["text_out"] * settings.price_text_out) / 1_000_000,
            4,
        )

    def meta(self) -> dict[str, Any]:
        return {
            "model": settings.gemini_live_model,
            "language": self.lang,
            "started_at": self.started_iso,
            "ended_at": now_iso(),
            "duration_sec": round(time.monotonic() - self.started),
            "resumed_from_draft": self.draft.resumed,
            "usage_tokens": dict(self.usage),
            "estimated_cost_usd": self.cost_usd(),
            "tool_calls": len(self.draft.tool_log),
        }

    def _add_usage(self, um: types.UsageMetadata) -> None:
        """Summed per turn. Each turn's prompt count already covers the whole
        context, which is also how it is billed, so the sum is the estimate."""
        self.usage["turns"] += 1
        for detail, prefix in ((um.prompt_tokens_details, "in"), (um.response_tokens_details, "out")):
            for d in detail or []:
                modality = str(getattr(d.modality, "value", d.modality) or "").upper()
                kind = "audio" if "AUDIO" in modality else "text"
                self.usage[f"{kind}_{prefix}"] += d.token_count or 0
        self.usage["text_out"] += um.thoughts_token_count or 0

    def _save_final(self, draft: ReportDraft) -> dict[str, Any]:
        self._flush()
        report_id = f"R-{datetime.now():%Y%m%d-%H%M%S}-{self.job['job_id']}"
        report = draft.to_report(report_id, self.meta())
        storage.save_report(report)
        storage.delete_draft(self.job["job_id"])
        return report

    def _input_transcription(self, with_language: bool) -> types.AudioTranscriptionConfig:
        speech = SPEECH_INPUT.get(self.lang, SPEECH_INPUT["ja"])
        vocab = self.draft.rules.vocabulary_for(self.job["large"]) if speech["japanese_vocabulary"] else []
        return types.AudioTranscriptionConfig(
            language_codes=speech["language_codes"] if with_language else None,
            custom_vocabulary=vocab or None,
        )

    def _config(self, with_language: bool = True) -> types.LiveConnectConfig:
        behavior = (
            types.Behavior.NON_BLOCKING
            if "extended-thinking" in settings.gemini_live_model  # only mode that model supports
            else types.Behavior.BLOCKING  # wait for the checklist before asking the next question
        )
        return types.LiveConnectConfig(
            response_modalities=[types.Modality.AUDIO],
            system_instruction=build_system_instruction(self.draft, self.lang),
            tools=[types.Tool(function_declarations=build_declarations(self.draft, behavior))],
            input_audio_transcription=self._input_transcription(with_language),
            output_audio_transcription=types.AudioTranscriptionConfig(),
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=settings.gemini_voice)
                )
            ),
            context_window_compression=types.ContextWindowCompressionConfig(sliding_window=types.SlidingWindow()),
        )

    # ------------------------------------------------------------ main

    async def run(self) -> None:
        await self.send_state()
        if not settings.gemini_api_key:
            await self.send_json({"type": "error", "message": "GEMINI_API_KEY が設定されていません (.env)"})
            return
        client = genai.Client(api_key=settings.gemini_api_key, http_options={"api_version": "v1beta"})
        try:
            stack, session = await self._open(client)
            async with stack:
                self.session = session
                await self.send_json({"type": "status", "status": "connected", "model": settings.gemini_live_model})
                await session.send_realtime_input(text=KICKOFF)
                tasks = [
                    asyncio.create_task(self._from_browser(), name="browser"),
                    asyncio.create_task(self._from_gemini(), name="gemini"),
                    asyncio.create_task(self._timer(), name="timer"),
                ]
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                for task in done:
                    if task.exception() is not None:
                        raise task.exception()
        except Exception as exc:
            logger.exception("live session failed")
            await self.send_json({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
        finally:
            await self._wrap_up()

    async def _open(self, client: genai.Client) -> tuple[contextlib.AsyncExitStack, Any]:
        """Connect with the input-language hint; if the setup is refused,
        connect once more without it (a refused setup bills no tokens)."""
        for with_language in (True, False):
            stack = contextlib.AsyncExitStack()
            try:
                session = await stack.enter_async_context(
                    client.aio.live.connect(model=settings.gemini_live_model, config=self._config(with_language))
                )
                return stack, session
            except Exception as exc:
                await stack.aclose()
                if not with_language:
                    raise
                logger.warning("setup with language_codes refused, retrying without: %s", exc)
                await self.send_json({"type": "status", "status": "language_hint_unsupported", "detail": str(exc)})
        raise RuntimeError("unreachable")

    async def _wrap_up(self) -> None:
        self._flush()
        report = self.draft.finalized_report
        if report is not None:
            # Re-save with the closing words and the final usage.
            report["transcript"] = self.draft.transcript
            report["conversation"] = self.meta()
            storage.save_report(report)
        elif self.draft.values or self.draft.cost_items or self.draft.exemptions:
            storage.save_draft(self.job["job_id"], self.draft.to_draft())
            await self.send_json({"type": "draft_saved"})
        await self.send_json({"type": "ended", "meta": self.meta(), "report_id": (report or {}).get("report_id")})
        logger.info("call ended job=%s meta=%s", self.job["job_id"], self.meta())

    async def _from_browser(self) -> None:
        while True:
            msg = await self.ws.receive()
            if msg["type"] == "websocket.disconnect":
                self.ws_open = False
                return
            if msg.get("bytes"):
                await self.session.send_realtime_input(
                    audio=types.Blob(data=msg["bytes"], mime_type="audio/pcm;rate=16000")
                )
                continue
            if not msg.get("text"):
                continue
            data = json.loads(msg["text"])
            kind = data.get("type")
            if kind == "text" and (data.get("text") or "").strip():
                self._flush()
                self.draft.transcript.append({"role": "worker", "text": data["text"].strip(), "at": now_iso(), "typed": True})
                await self.session.send_realtime_input(text=data["text"].strip())
            elif kind == "audio_end":
                await self.session.send_realtime_input(audio_stream_end=True)
            elif kind == "stop":
                return

    async def _from_gemini(self) -> None:
        while True:
            got_any = False
            async for msg in self.session.receive():  # yields one turn, then ends
                got_any = True
                await self._handle(msg)
            if self.end_after_turn and self.spoke_after_final:
                return  # closing words were said
            if not got_any:
                return  # stream closed

    async def _timer(self) -> None:
        limit = settings.max_session_seconds
        warned = False
        while True:
            await asyncio.sleep(1)
            elapsed = time.monotonic() - self.started
            if self.finalized_at and time.monotonic() - self.finalized_at > _FINALIZE_GRACE_SECONDS:
                return
            if not warned and elapsed > limit - 60:
                warned = True
                await self.session.send_realtime_input(text=TIME_WARNING)
                await self.send_json({"type": "status", "status": "time_warning"})
            if elapsed > limit:
                await self.send_json({"type": "status", "status": "time_limit"})
                return

    async def _handle(self, msg: types.LiveServerMessage) -> None:
        if msg.usage_metadata:
            self._add_usage(msg.usage_metadata)
            await self.send_json({"type": "usage", "usage": self.usage, "cost_usd": self.cost_usd()})

        sc = msg.server_content
        if sc:
            if sc.input_transcription and sc.input_transcription.text:
                if self.agent_buf:
                    self._commit("agent")
                self.worker_buf += sc.input_transcription.text
                await self.send_json({"type": "transcript", "role": "worker", "delta": sc.input_transcription.text})
            if sc.output_transcription and sc.output_transcription.text:
                if self.worker_buf:
                    self._commit("worker")
                self.agent_buf += sc.output_transcription.text
                await self.send_json({"type": "transcript", "role": "agent", "delta": sc.output_transcription.text})
            if sc.model_turn and sc.model_turn.parts:
                for part in sc.model_turn.parts:
                    if part.inline_data and part.inline_data.data and self.ws_open:
                        if self.finalized_at:
                            self.spoke_after_final = True
                        try:
                            await self.ws.send_bytes(part.inline_data.data)
                        except Exception:
                            self.ws_open = False
            if sc.interrupted:
                await self.send_json({"type": "interrupted"})
            if sc.turn_complete:
                self._flush()
                await self.send_json({"type": "turn_complete"})

        if msg.tool_call and msg.tool_call.function_calls:
            responses = []
            for fc in msg.tool_call.function_calls:
                result = self.tools(fc.name, fc.args)
                await self.send_json({"type": "tool", "name": fc.name, "args": fc.args, "result": result})
                responses.append(types.FunctionResponse(id=fc.id, name=fc.name, response=result))
            await self.session.send_tool_response(function_responses=responses)
            await self.send_state()
            report = self.draft.finalized_report
            if report is not None and not self.end_after_turn:
                self.end_after_turn = True
                self.finalized_at = time.monotonic()
                await self.send_json({"type": "finalized", "report_id": report["report_id"]})

        if msg.tool_call_cancellation:
            logger.info("tool calls cancelled: %s", msg.tool_call_cancellation.ids)
        if msg.go_away:
            await self.send_json({"type": "status", "status": "go_away", "time_left": str(msg.go_away.time_left)})
