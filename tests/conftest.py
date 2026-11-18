import pytest

from app.core.config import settings
from app.services import storage
from app.services.report_draft import ReportDraft
from app.services.rules import load_rules


@pytest.fixture
def rules():
    return load_rules(settings.rules_path)


@pytest.fixture
def aircon_job():
    return storage.get_job("J-1001")


@pytest.fixture
def draft(rules, aircon_job):
    return ReportDraft(aircon_job, rules)


@pytest.fixture(autouse=True)
def tmp_storage(tmp_path, monkeypatch):
    """Reports and drafts go to a temp dir — tests never touch data/."""
    monkeypatch.setattr(settings, "reports_dir", tmp_path / "reports")
    monkeypatch.setattr(settings, "drafts_dir", tmp_path / "drafts")
    return tmp_path


def fill_aircon(draft: ReportDraft) -> None:
    """Every aircon field answered well enough to pass."""
    draft.set_values({
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
        "unit_maker_model": "ダイキン SZRC112BD",
        "error_code": "E7",
        "refrigerant": "補充なし",
    })
    draft.set_cost_items([
        {"label": "ファンモーター", "cost_group": "material", "amount": 32000},
        {"label": "ファンモーター交換作業", "cost_group": "labor", "amount": 18000},
        {"label": "出張費", "cost_group": "support", "amount": 5000},
    ])
