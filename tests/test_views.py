"""Smoke tests: every page renders and the main buttons work through the web interface."""

from django.urls import reverse

from planner.models import Experiment, SequencingRun

from .conftest import sample_rows


def csv_upload(rows):
    from django.core.files.uploadedfile import SimpleUploadedFile
    header = list(rows[0])
    text = ";".join(header) + "\n" + "\n".join(";".join(r[h] for h in header) for r in rows)  # German Excel style
    return SimpleUploadedFile("samples.csv", ("﻿" + text).encode("utf-8"), content_type="text/csv")


def test_pages_require_login(client):
    assert client.get(reverse("dashboard")).status_code == 302


def test_click_through(client, users, tp_experiment, index_set, flowcell):
    client.force_login(users["tech"])
    assert client.get(reverse("dashboard")).status_code == 200
    url = reverse("experiment_detail", args=[tp_experiment.pk])
    assert client.get(url).status_code == 200

    r = client.post(reverse("experiment_upload", args=[tp_experiment.pk]), {"file": csv_upload(sample_rows(30))})
    assert r.status_code == 302 and tp_experiment.samples.count() == 30
    client.post(reverse("experiment_plan", args=[tp_experiment.pk]), {"max_pool_size": 15, "shuffle": "on", "seed": 9})
    assert tp_experiment.pools.count() == 2
    client.post(reverse("experiment_action", args=[tp_experiment.pk, "accept"]))
    client.post(reverse("experiment_action", args=[tp_experiment.pk, "submit_plan"]))
    tp_experiment.refresh_from_db()
    assert tp_experiment.status == Experiment.Status.PLAN_IN_REVIEW
    page = client.get(url).content.decode()
    assert "Waiting for a second person" in page

    client.force_login(users["alex"])
    so = tp_experiment.signoffs.get(step="barcode_plan")
    client.post(reverse("experiment_action", args=[tp_experiment.pk, "approve_plan"]), {"signoff": so.pk})
    tp_experiment.refresh_from_db()
    assert tp_experiment.status == Experiment.Status.PLAN_APPROVED
    r = client.get(reverse("experiment_download", args=[tp_experiment.pk, "star"]))
    assert r.status_code == 200 and r.content.decode().startswith("sample_id,source_plate")
    assert client.get(reverse("experiment_history", args=[tp_experiment.pk])).status_code == 200

    r = client.post(reverse("run_new"), {"run_id": "NGS-9", "flowcell_type": flowcell.pk})
    run = SequencingRun.objects.get(run_id="NGS-9")
    client.post(reverse("run_action", args=[run.pk, "add_experiment"]), {"experiment": tp_experiment.pk})
    client.post(reverse("run_action", args=[run.pk, "assign_indexes"]), {"index_set": index_set.pk})
    page = client.get(reverse("run_detail", args=[run.pk])).content.decode()
    assert "Flow cell options" in page and "ERROR" not in page
    for kind in ("samplesheet", "provider", "barcode_map", "json"):
        assert client.get(reverse("run_download", args=[run.pk, kind])).status_code == 200


def test_error_message_shown(client, users, tp_experiment):
    client.force_login(users["tech"])
    url = reverse("experiment_plan", args=[tp_experiment.pk])
    r = client.post(url, {"shuffle": "on"}, HTTP_REFERER=reverse("experiment_detail", args=[tp_experiment.pk]), follow=True)
    assert "Upload a sample list first" in r.content.decode()
