"""JSON-file storage for the demo: jobs (read-only), drafts, submitted reports."""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from app.core.config import settings

_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]+$")


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, data: Any) -> None:
    """Write via a temp file + rename, so a crash never leaves half a report."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _safe(ident: str) -> str:
    if not _SAFE_ID.match(ident or ""):
        raise ValueError(f"invalid id: {ident!r}")
    return ident


# ---------------------------------------------------------------- jobs

def list_jobs() -> list[dict[str, Any]]:
    return _read(settings.jobs_path)["jobs"]


def get_job(job_id: str) -> dict[str, Any] | None:
    return next((j for j in list_jobs() if j["job_id"] == job_id), None)


# ---------------------------------------------------------------- drafts

def _draft_path(job_id: str) -> Path:
    return settings.drafts_dir / f"{_safe(job_id)}.json"


def save_draft(job_id: str, draft: dict[str, Any]) -> None:
    _write(_draft_path(job_id), draft)


def load_draft(job_id: str) -> dict[str, Any] | None:
    path = _draft_path(job_id)
    return _read(path) if path.exists() else None


def delete_draft(job_id: str) -> None:
    _draft_path(job_id).unlink(missing_ok=True)


# ---------------------------------------------------------------- reports

def save_report(report: dict[str, Any]) -> None:
    _write(settings.reports_dir / f"{_safe(report['report_id'])}.json", report)


def get_report(report_id: str) -> dict[str, Any] | None:
    path = settings.reports_dir / f"{_safe(report_id)}.json"
    return _read(path) if path.exists() else None


def list_reports() -> list[dict[str, Any]]:
    if not settings.reports_dir.exists():
        return []
    reports = [_read(p) for p in settings.reports_dir.glob("*.json")]
    return sorted(reports, key=lambda r: r.get("submitted_at", ""), reverse=True)


def latest_report_for_job(job_id: str) -> dict[str, Any] | None:
    return next((r for r in list_reports() if r["job"]["job_id"] == job_id), None)
