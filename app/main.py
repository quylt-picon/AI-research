"""Demo app: worker report by voice (Gemini Live) for QNavi Approve Helper.

    uv run uvicorn app.main:app --reload --port 8100
    open http://localhost:8100
"""

import logging

from fastapi import FastAPI, HTTPException, WebSocket
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from app.core.config import ROOT, settings
from app.services import storage
from app.services.live_bridge import LiveCall
from app.services.prompt import LANGUAGES, build_system_instruction
from app.services.report_draft import ReportDraft
from app.services.rules import RulesError, RulesStore

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

app = FastAPI(title="Worker Report Live (demo)")
rules_store = RulesStore(settings.rules_path)
STATIC = ROOT / "static"


def _job_or_404(job_id: str) -> dict:
    job = storage.get_job(job_id)
    if job is None:
        raise HTTPException(404, f"job {job_id} not found")
    return job


def _draft_for(job: dict) -> ReportDraft:
    draft = ReportDraft(job, rules_store.current)
    prior = storage.load_draft(job["job_id"])
    if prior:
        draft.load_draft(prior)
    return draft


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/config")
def config() -> dict:
    return {
        "model": settings.gemini_live_model,
        "api_key_configured": bool(settings.gemini_api_key),
        "max_session_seconds": settings.max_session_seconds,
        "languages": list(LANGUAGES),
        "prices_per_1m_usd": {
            "audio_in": settings.price_audio_in, "text_in": settings.price_text_in,
            "audio_out": settings.price_audio_out, "text_out": settings.price_text_out,
        },
    }


@app.get("/api/jobs")
def jobs() -> list[dict]:
    out = []
    for job in storage.list_jobs():
        report = storage.latest_report_for_job(job["job_id"])
        draft = storage.load_draft(job["job_id"])
        status = "reported" if report else "draft" if draft else "open"
        out.append({**job, "status": status, "report_id": (report or {}).get("report_id")})
    return out


@app.get("/api/jobs/{job_id}/checklist")
def job_checklist(job_id: str) -> dict:
    """What the worker will be asked for this job, before calling."""
    draft = _draft_for(_job_or_404(job_id))
    return {"state": draft.state(), "resumed": draft.resumed,
            "fields": [{"key": f.key, "label": f.label, "source": f.source, "required": f.required,
                        "required_when": f.required_when, "waivable": f.waivable} for f in draft.fields]}


@app.delete("/api/jobs/{job_id}/draft")
def discard_draft(job_id: str) -> dict:
    _job_or_404(job_id)
    storage.delete_draft(job_id)
    return {"ok": True}


@app.get("/api/reports")
def reports() -> list[dict]:
    return [
        {k: r.get(k) for k in ("report_id", "submitted_at", "completeness", "cost_total")}
        | {"job": r["job"], "flags": len(r.get("approver_flags") or []),
           "exemptions": len(r.get("exemptions") or [])}
        for r in storage.list_reports()
    ]


@app.get("/api/reports/{report_id}")
def report(report_id: str) -> dict:
    try:
        found = storage.get_report(report_id)
    except ValueError:
        found = None
    if found is None:
        raise HTTPException(404, "report not found")
    return found


@app.get("/api/rules")
def rules() -> dict:
    current = rules_store.current
    cats = current.raw.get("categories") or {}
    return {
        "version": current.version,
        "path": str(current.path.relative_to(ROOT)),
        "yaml": current.text,
        "categories": {k: v.get("vi", "") for k, v in cats.items()},
    }


@app.post("/api/rules/reload")
def reload_rules() -> dict:
    try:
        current = rules_store.reload()
    except RulesError as exc:
        raise HTTPException(400, f"Rule lỗi, vẫn giữ bản cũ: {exc}") from exc
    return {"ok": True, "version": current.version}


@app.get("/api/jobs/{job_id}/prompt", response_class=PlainTextResponse)
def prompt(job_id: str, lang: str = "ja") -> str:
    """The exact system instruction Gemini would get for this job."""
    return build_system_instruction(_draft_for(_job_or_404(job_id)), lang)


@app.websocket("/ws/report/{job_id}")
async def ws_report(ws: WebSocket, job_id: str, lang: str = "ja") -> None:
    await ws.accept()
    job = storage.get_job(job_id)
    if job is None:
        await ws.send_json({"type": "error", "message": f"job {job_id} not found"})
        await ws.close()
        return
    if storage.latest_report_for_job(job_id):
        await ws.send_json({"type": "error", "message": "この案件は報告済みです"})
        await ws.close()
        return
    call = LiveCall(ws, job, rules_store.current, lang if lang in LANGUAGES else "ja")
    await call.run()
    try:
        await ws.close()
    except Exception:
        pass


app.mount("/static", StaticFiles(directory=STATIC), name="static")
