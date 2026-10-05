"""End-to-end tests of the planning workflow, sign-offs and exports."""

import csv
import io
import sys
from pathlib import Path

import pytest

from planner import exports, services
from planner.models import Experiment, Sample, SequencingRun, SignOff
from planner.services import PlannerError

from .conftest import sample_rows


def approve_plan(exp, users):
    services.accept_experiment(exp, users["bioinf"])
    so = services.submit_plan(exp, users["tech"])
    services.decide_plan(so, users["alex"], approve=True)
    exp.refresh_from_db()


def test_even_pool_sizes():
    assert services.even_pool_sizes(60, 24) == [20, 20, 20]
    assert services.even_pool_sizes(50, 24) == [17, 17, 16]
    assert services.even_pool_sizes(24, 24) == [24]


def test_plan_barcodes_unique_per_pool_and_reproducible(tp_experiment, users):
    services.upload_samples(tp_experiment, sample_rows(60), users["tech"])
    pools = services.plan_barcodes(tp_experiment, seed=123)
    assert [p.samples.count() for p in pools] == [20, 20, 20]
    for p in pools:
        bcs = list(p.samples.values_list("well_barcode__barcode_id", flat=True))
        assert len(bcs) == len(set(bcs))
    first = {s.sample_id: s.well_barcode.barcode_id for s in tp_experiment.samples.all()}
    services.plan_barcodes(tp_experiment, seed=123)
    again = {s.sample_id: s.well_barcode.barcode_id for s in tp_experiment.samples.all()}
    assert first == again
    assert services.validate_plan(tp_experiment) == []


def test_explicit_pool_sizes_validated(tp_experiment, users):
    services.upload_samples(tp_experiment, sample_rows(30), users["tech"])
    with pytest.raises(PlannerError):
        services.plan_barcodes(tp_experiment, pool_sizes=[25, 5])  # 25 > 24 barcodes
    with pytest.raises(PlannerError):
        services.plan_barcodes(tp_experiment, pool_sizes=[10, 10])  # sum != 30
    pools = services.plan_barcodes(tp_experiment, pool_sizes=[24, 6])
    assert [p.samples.count() for p in pools] == [24, 6]


def test_four_eyes_and_lock(tp_experiment, users):
    services.upload_samples(tp_experiment, sample_rows(24), users["tech"])
    services.plan_barcodes(tp_experiment, seed=1)
    services.accept_experiment(tp_experiment, users["bioinf"])
    so = services.submit_plan(tp_experiment, users["tech"])
    with pytest.raises(PlannerError, match="4-eyes"):
        services.decide_plan(so, users["tech"], approve=True)
    with pytest.raises(PlannerError, match="locked"):
        services.plan_barcodes(tp_experiment)
    services.decide_plan(so, users["alex"], approve=True)
    tp_experiment.refresh_from_db()
    assert tp_experiment.status == Experiment.Status.PLAN_APPROVED
    with pytest.raises(PlannerError):
        services.reopen_plan(tp_experiment, users["alex"], "")
    services.reopen_plan(tp_experiment, users["alex"], "wrong culture plate order")
    tp_experiment.refresh_from_db()
    assert tp_experiment.status == Experiment.Status.ACCEPTED
    assert tp_experiment.deviations.count() == 1
    assert tp_experiment.signoffs.get(pk=so.pk).state == SignOff.State.SUPERSEDED


def test_change_after_submission_blocks_approval(tp_experiment, users):
    services.upload_samples(tp_experiment, sample_rows(24), users["tech"])
    services.plan_barcodes(tp_experiment, seed=1)
    services.accept_experiment(tp_experiment, users["bioinf"])
    so = services.submit_plan(tp_experiment, users["tech"])
    s = Sample.objects.get(experiment=tp_experiment, sample_id="S001")
    s.source_well = "D6"  # someone edits behind the app's back
    s.save()
    with pytest.raises(PlannerError, match="changed after"):
        services.decide_plan(so, users["alex"], approve=True)


def test_history_records_user(tp_experiment, users):
    services.upload_samples(tp_experiment, sample_rows(5), users["tech"])
    tp_experiment.title = "renamed"
    tp_experiment.save()
    assert tp_experiment.history.count() >= 2


def test_star_export_passes_robot_validator(tp_experiment, users, tmp_path):
    """The exported CSV must be accepted by the STAR simulation's own sample-sheet checks."""
    sim = Path(__file__).resolve().parents[2] / "tag_and_pool_hamilton_star_sim" / "src"
    if not sim.exists():
        pytest.skip("simulation repo not checked out next to this repo")
    pytest.importorskip("pandas")
    sys.path.insert(0, str(sim))
    from libprep.samplesheet import load_barcode_plate, load_sample_sheet, plan_layout

    services.upload_samples(tp_experiment, sample_rows(240), users["tech"])
    services.plan_barcodes(tp_experiment, seed=7)
    approve_plan(tp_experiment, users)
    (tmp_path / "s.csv").write_text(exports.star_sample_sheet(tp_experiment))
    (tmp_path / "b.csv").write_text(exports.star_barcode_plate(tp_experiment.well_barcode_set))
    bc = load_barcode_plate(tmp_path / "b.csv")
    df = load_sample_sheet(tmp_path / "s.csv", bc)
    layout = plan_layout(df, bc)
    assert len(layout.pools) == 10


def test_manifest_comparison(tp_experiment, users):
    services.upload_samples(tp_experiment, sample_rows(24), users["tech"])
    services.plan_barcodes(tp_experiment, seed=3)
    approve_plan(tp_experiment, users)
    rows = list(csv.DictReader(io.StringIO(exports.star_sample_sheet(tp_experiment))))
    rows[0]["barcode_id"] = "BC99"
    diffs = services.record_libprep_done(tp_experiment, users["tech"], manifest_rows=rows)
    assert len(diffs) == 1 and "S0" in diffs[0]
    assert tp_experiment.deviations.count() == 1
    tp_experiment.refresh_from_db()
    assert tp_experiment.status == Experiment.Status.LIBPREP_DONE


def test_full_run_workflow(tp_experiment, users, index_set, flowcell):
    services.upload_samples(tp_experiment, sample_rows(60), users["tech"])
    services.plan_barcodes(tp_experiment, seed=5)
    approve_plan(tp_experiment, users)
    services.record_libprep_done(tp_experiment, users["tech"])

    bulk = Experiment.objects.create(code="RNA-1", title="bulk", library_type="bulk_rnaseq", r1_length=28, r2_length=90,
                                     i7_length=8, i5_length=8, requested_m_read_pairs=120)
    services.upload_samples(bulk, sample_rows(6), users["tech"])
    assert bulk.pools.count() == 6  # one library per sample
    services.accept_experiment(bulk, users["bioinf"])

    run = SequencingRun.objects.create(run_id="NGS-1", flowcell_type=flowcell)
    services.add_experiment_to_run(run, tp_experiment)
    services.add_experiment_to_run(run, bulk)
    run.refresh_from_db()
    assert run.read_structure == "28-8-8-90"
    assert sum(rp.target_m_read_pairs for rp in run.run_pools.all()) == pytest.approx(720)
    assert services.assign_indexes(run, index_set) == 9
    pairs = {(rp.pool.i7_id, rp.pool.i5_id) for rp in run.run_pools.all()}
    i7s = {rp.pool.i7_id for rp in run.run_pools.all()}
    assert len(pairs) == 9 and len(i7s) == 9  # ≤12 libraries → unique i7 (and i5) each
    errors = [i for i in services.check_run(run) if i.level == "error"]
    assert not errors, errors

    so = services.submit_run(run, users["bioinf"])
    services.decide_run(so, users["alex"], approve=True)
    tp_experiment.refresh_from_db()
    assert tp_experiment.status == Experiment.Status.RUN_PLANNED
    with pytest.raises(PlannerError, match="locked"):
        services.add_experiment_to_run(run, bulk)

    sheet = exports.illumina_samplesheet_v2(run)
    assert "[BCLConvert_Data]" in sheet and "Index1Cycles,8" in sheet
    assert sheet.count("TP-1_P0") == 3
    bmap = list(csv.DictReader(io.StringIO(exports.barcode_map_run(run))))
    assert len(bmap) == 66
    tp_rows = [r for r in bmap if r["experiment"] == "TP-1"]
    assert all(r["well_barcode_read"] == "R2" and r["well_barcode_length"] == "8" for r in tp_rows)

    services.mark_run_submitted(run, users["bioinf"])
    services.mark_data_delivered(run, users["bioinf"])
    bulk.refresh_from_db()
    assert bulk.status == Experiment.Status.DATA_DELIVERED


def test_tag_and_pool_needs_approved_plan_for_run(tp_experiment, users, flowcell):
    services.upload_samples(tp_experiment, sample_rows(24), users["tech"])
    services.plan_barcodes(tp_experiment, seed=1)
    services.accept_experiment(tp_experiment, users["bioinf"])
    run = SequencingRun.objects.create(run_id="NGS-2", flowcell_type=flowcell)
    with pytest.raises(PlannerError, match="barcode plan"):
        services.add_experiment_to_run(run, tp_experiment)


def test_i5_reverse_complement_option(tp_experiment, users, index_set, flowcell):
    services.upload_samples(tp_experiment, sample_rows(10), users["tech"])
    services.plan_barcodes(tp_experiment, seed=1)
    approve_plan(tp_experiment, users)
    run = SequencingRun.objects.create(run_id="NGS-3", flowcell_type=flowcell)
    services.add_experiment_to_run(run, tp_experiment)
    services.assign_indexes(run, index_set)
    i5 = run.run_pools.first().pool.i5.sequence
    assert f",{i5}" in exports.illumina_samplesheet_v2(run)
    run.i5_reverse_complement = True
    run.save()
    assert f",{exports.revcomp(i5)}" in exports.illumina_samplesheet_v2(run)
