"""SharePoint sync logic with a fake Graph client (no network)."""

import pytest

from planner import sharepoint
from planner.models import Experiment

F = sharepoint.field_map()["fields"]


class FakeClient:
    def __init__(self, items):
        self._items = items
        self.patched = []

    def items(self):
        return self._items

    def user_email(self, lookup_id):
        return {"7": "tina@example.org"}.get(str(lookup_id), "")

    def update_fields(self, item_id, fields):
        self.patched.append((item_id, fields))


def item(item_id, **over):
    fields = {F["title"]: "iNeuron screen", F["library_type"]: "Tag&Pool", F["r1_length"]: 28.0, F["r2_length"]: 90.0,
              F["i7_length"]: 8, F["i5_length"]: 8, F["requested_m_read_pairs"]: 400,
              F["lysis_date"]: "2026-11-02T00:00:00Z", F["library_complexity"]: "normal",
              F["eln_assay_url"]: {"Url": "https://eln.example.org/x", "Description": "x"},
              f"{F['responsible_libprep']}LookupId": "7"}
    fields.update(over)
    return {"id": str(item_id), "fields": fields}


@pytest.fixture
def fake(monkeypatch):
    client = FakeClient([item(12)])
    monkeypatch.setattr(sharepoint, "GraphClient", lambda: client)
    monkeypatch.setattr(sharepoint, "configured", lambda: True)
    return client


def test_pull_creates_and_updates(db, fake):
    assert sharepoint.pull(dry_run=True)[0].startswith("NEW")
    assert not Experiment.objects.exists()
    sharepoint.pull()
    e = Experiment.objects.get(sharepoint_item_id=12)
    assert e.library_type == "tag_and_pool" and e.r1_length == 28 and e.lysis_date.isoformat() == "2026-11-02"
    assert e.responsible_libprep.email == "tina@example.org"
    assert e.eln_assay_url == "https://eln.example.org/x"
    fake._items = [item(12, **{F["requested_m_read_pairs"]: 500})]
    assert "requested_m_read_pairs" in sharepoint.pull()[0]
    e.refresh_from_db()
    assert e.requested_m_read_pairs == 500


def test_locked_plan_ignores_read_structure_changes(db, fake):
    sharepoint.pull()
    e = Experiment.objects.get(sharepoint_item_id=12)
    e.status = Experiment.Status.PLAN_APPROVED
    e.save()
    fake._items = [item(12, **{F["r2_length"]: 150})]
    sharepoint.pull()
    e.refresh_from_db()
    assert e.r2_length == 90


def test_status_change_pushes(db, fake, users):
    from planner import services
    sharepoint.pull()
    e = Experiment.objects.get(sharepoint_item_id=12)
    services.accept_experiment(e, users["bioinf"])
    assert fake.patched[-1] == (12, {"Status": "Accepted – in planning"})
    e.refresh_from_db()
    assert e.sharepoint_push_pending is False
