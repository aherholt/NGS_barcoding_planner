"""Hand-over of the next step to a person (shown in the app, pushed to SharePoint for Power Automate)."""

import pytest
from django.urls import reverse

from planner import services
from planner.models import Experiment, SequencingRun
from planner.services import AUTO, PlannerError

from .conftest import sample_rows


def _plan(exp, users):
    services.upload_samples(exp, sample_rows(24), users["tech"])
    services.plan_barcodes(exp, seed=1)


def test_defaults_follow_the_workflow(tp_experiment, users, index_set, flowcell):
    tp_experiment.responsible_libprep = users["tech"]
    tp_experiment.responsible_ngs = users["bioinf"]
    tp_experiment.save()
    _plan(tp_experiment, users)
    assert services.next_step(tp_experiment) == ("Accept experiment", users["bioinf"])  # new request → NGS orga
    services.accept_experiment(tp_experiment, users["alex"])
    assert services.next_step(tp_experiment) == ("Plan samples/pools and add to an NGS run", users["tech"])

    run = SequencingRun.objects.create(run_id="NGS-H1", flowcell_type=flowcell)
    services.add_experiment_to_run(run, tp_experiment)
    run.refresh_from_db()
    tp_experiment.refresh_from_db()
    assert run.assignee == users["bioinf"]  # run planning → responsible person NGS
    label, owner = services.next_step(tp_experiment)
    assert "submit final plan" in label and owner == users["bioinf"] and services.next_step_on_run(tp_experiment)

    services.assign_indexes(run, index_set)
    with pytest.raises(PlannerError, match="4-eyes"):
        services.submit_run(run, users["bioinf"], assignee=users["bioinf"])
    so = services.submit_run(run, users["bioinf"], assignee=users["alex"])
    tp_experiment.refresh_from_db()
    assert services.next_step(tp_experiment) == ("Approve final plan (run NGS-H1)", users["alex"])

    services.decide_run(so, users["alex"], approve=True)  # AUTO → each experiment's responsible person
    tp_experiment.refresh_from_db()
    assert services.next_step(tp_experiment) == ("Library prep", users["tech"])

    services.record_libprep_done(tp_experiment, users["tech"], assignee=users["bioinf"])
    tp_experiment.refresh_from_db()
    assert services.next_step(tp_experiment) == ("Submit run to provider (run NGS-H1)", users["bioinf"])
    services.mark_run_submitted(run, users["bioinf"])
    services.mark_data_delivered(run, users["bioinf"])
    tp_experiment.refresh_from_db()
    run.refresh_from_db()
    assert services.next_step(tp_experiment) == ("", None) and run.assignee is None


def test_explicit_choice_and_nobody(tp_experiment, users):
    services.accept_experiment(tp_experiment, users["alex"], assignee=users["bioinf"])
    tp_experiment.refresh_from_db()
    assert tp_experiment.assignee == users["bioinf"]
    services.assign(tp_experiment, None)
    tp_experiment.refresh_from_db()
    assert services.next_step(tp_experiment)[1] is None
    assert tp_experiment.history.filter(assignee=users["bioinf"]).exists()  # audit trail keeps the change


def test_pages_show_owner_and_my_tasks(client, users, tp_experiment):
    _plan(tp_experiment, users)
    client.force_login(users["alex"])
    url = reverse("experiment_detail", args=[tp_experiment.pk])
    assert "Hand over planning to" in client.get(url).content.decode()
    client.post(reverse("experiment_action", args=[tp_experiment.pk, "accept"]), {"assignee": users["tech"].pk})
    page = client.get(url).content.decode()
    assert "Next:" in page and "tech" in page
    client.force_login(users["tech"])
    dash = client.get(reverse("dashboard")).content.decode()
    assert "My tasks" in dash and tp_experiment.code in dash.split("My tasks")[1].split("</div>\n\n")[0]
    client.post(reverse("experiment_action", args=[tp_experiment.pk, "assign"]), {"assignee": ""})
    tp_experiment.refresh_from_db()
    assert tp_experiment.assignee is None
