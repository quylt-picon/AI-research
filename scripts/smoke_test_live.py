"""Real Gemini Live smoke test — COSTS MONEY (small). Ask before running.

Plays a worker by text (no mic) through the real bridge (LiveCall), against
the real model, then prints the conversation, every tool call, the token
usage and the estimated cost.

    uv run python scripts/smoke_test_live.py                 # report goes to a temp dir
    uv run python scripts/smoke_test_live.py --save          # report goes to data/reports
    uv run python scripts/smoke_test_live.py --job J-1003

Hard limits: --max-turns worker turns (default 8) and --max-seconds (default 150).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings  # noqa: E402
from app.services import storage  # noqa: E402
from app.services.live_bridge import LiveCall  # noqa: E402
from app.services.rules import load_rules  # noqa: E402

# Each line answers several fields at once, the way a real worker talks.
WORKER_LINES = {
    "J-1001": [
        "室外機のファンが回ってなくて、リモコンにE7が出てました。ファンモーターが焼けてたので交換して、30分試運転して冷えるのを確認しました。原因はモーターの経年劣化です。",
        "メーカーはダイキンですけど、型番は銘板が油で汚れてて読めなかったです。",
        "ガスの補充はしてないです。部材はファンモーター1個です。写真は作業前後で撮ってあります。店長の佐藤さんに説明しました。",
        "費用は、ファンモーターが3万2千円、交換作業が1万8千円、出張費が5千円です。全部税抜です。",
    ],
    "J-1003": [
        "入口横のFIXの網入りガラスが下の角から斜めに割れてました。昨日の夜に自転車がぶつかったって店長さんが言ってました。誰のかは分からないそうです。",
        "今日は飛散防止テープで養生して、注意の貼り紙をしました。ガラスは寸法を測って発注したので、来週交換に行きます。",
        "網入り透明ガラスの6.8ミリで、幅900、高さ1800です。部材は養生テープだけです。写真は撮りました。店長の佐藤さんに説明済みです。",
        "費用は、養生作業が8千円、出張費が5千円です。ガラス代と交換作業は次回の見積で出します。",
    ],
}
CONFIRM = "はい、その内容で大丈夫です。お願いします。"


class ScriptedWorker:
    """Stands in for the browser WebSocket: answers after each agent turn."""

    def __init__(self, lines: list[str], max_turns: int):
        self.lines = list(lines)
        self.max_turns = max_turns
        self.turns = 0
        self.queue: asyncio.Queue = asyncio.Queue()
        self.events: list[dict] = []
        self.audio_bytes = 0
        self.ready = False

    async def receive(self) -> dict:
        return await self.queue.get()

    async def send_bytes(self, data: bytes) -> None:
        self.audio_bytes += len(data)

    async def send_text(self, text: str) -> None:
        event = json.loads(text)
        self.events.append(event)
        kind = event["type"]
        if kind == "transcript":
            print(event["delta"], end="", flush=True)
        elif kind == "tool":
            result = event["result"]
            print(f"\n   [tool] {event['name']} {json.dumps(event['args'], ensure_ascii=False)[:300]}")
            print(f"          ok={result.get('ok')} status={result.get('checklist_status')} "
                  f"remaining={[r['id'] for r in result.get('remaining', [])]}"
                  + (f" error={result['error']}" if result.get("error") else ""))
        elif kind == "turn_complete":
            print()
            await self._answer()
        elif kind in ("error", "status", "finalized", "draft_saved"):
            print(f"\n   [{kind}] {event}")

    async def _answer(self) -> None:
        if any(e["type"] == "finalized" for e in self.events):
            return
        if self.turns >= self.max_turns:
            print("\n   [limit] max worker turns reached — stopping")
            await self.queue.put({"type": "websocket.receive", "text": json.dumps({"type": "stop"})})
            return
        self.turns += 1
        line = self.lines.pop(0) if self.lines else CONFIRM
        print(f"\n作業者> {line}\nオペレーター> ", end="")
        await self.queue.put({"type": "websocket.receive", "text": json.dumps({"type": "text", "text": line})})


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--job", default="J-1001", choices=sorted(WORKER_LINES))
    ap.add_argument("--save", action="store_true", help="save into data/ instead of a temp dir")
    ap.add_argument("--max-turns", type=int, default=8)
    ap.add_argument("--max-seconds", type=int, default=150)
    args = ap.parse_args()

    if not args.save:
        tmp = Path(tempfile.mkdtemp(prefix="smoke-live-"))
        settings.reports_dir, settings.drafts_dir = tmp / "reports", tmp / "drafts"
    settings.max_session_seconds = args.max_seconds + 60  # our own timeout below ends it first
    job = storage.get_job(args.job)
    if storage.latest_report_for_job(args.job):
        sys.exit(f"{args.job} already has a report in {settings.reports_dir}")

    worker = ScriptedWorker(WORKER_LINES[args.job], args.max_turns)
    call = LiveCall(worker, job, load_rules(settings.rules_path), "ja")
    print(f"model={settings.gemini_live_model} job={args.job} reports_dir={settings.reports_dir}\nオペレーター> ", end="")
    try:
        await asyncio.wait_for(call.run(), timeout=args.max_seconds)
    except asyncio.TimeoutError:
        # run()'s own `finally` already wrapped up (draft saved, session closed).
        print(f"\n   [limit] {args.max_seconds}s reached")

    meta = call.meta()
    print("\n" + "=" * 70)
    print(f"duration {meta['duration_sec']}s, tool calls {meta['tool_calls']}, audio received {worker.audio_bytes:,} bytes")
    print(f"tokens {meta['usage_tokens']}")
    print(f"estimated cost ${meta['estimated_cost_usd']:.4f}")
    report = call.draft.finalized_report
    if report:
        print(f"report {report['report_id']} → {settings.reports_dir}")
        print(json.dumps(report["qnavi_fields"], ensure_ascii=False, indent=2))
        print("flags:", json.dumps(report["approver_flags"], ensure_ascii=False, indent=2))
    else:
        print("not finalized; remaining:", [r["id"] for r in call.draft.checklist()["remaining"]])


if __name__ == "__main__":
    asyncio.run(main())
