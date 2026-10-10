"""Plate formats, PCR-plate layout, library structure, drag & drop services and endpoints."""

import json
import random
import sys
from pathlib import Path

import pytest
from django.urls import reverse

from planner import library, plates, services
from planner.models import Experiment, SequencingRun
from planner.services import PlannerError

from .conftest import sample_rows


# --- plates -----------------------------------------------------------------
def test_formats_and_parsing():
    assert plates.wells(6) == ["A1", "B1", "A2", "B2", "A3", "B3"]
    assert len(plates.wells(96)) == 96 and plates.wells(96)[8] == "A2"
    assert plates.parse_format("48-well") == 48 and plates.parse_format(" 12 ") == 12 and plates.parse_format("") is None
    with pytest.raises(ValueError):
        plates.parse_format("384")


def test_upload_plate_format_and_validation(tp_experiment, users):
    rows = [{"sample_id": f"S{i}", "source_plate": "P96", "source_well": plates.wells(96)[i], "plate_format": "96"}
            for i in range(30)]
    services.upload_samples(tp_experiment, rows, users["tech"])
    assert set(tp_experiment.samples.values_list("plate_format", flat=True)) == {96}
    services.plan_barcodes(tp_experiment, seed=1)
    assert not any("source_well" in p for p in services.validate_plan(tp_experiment))  # H12-type wells allowed
    assert any("24-well" in w for w in services.plan_warnings(tp_experiment))  # STAR supports 24-well only
    rows[0]["source_well"] = "E1"
    rows[0]["plate_format"] = "6"  # E1 does not exist in a 6-well plate; P96 now has two formats
    services.upload_samples(tp_experiment, rows, users["tech"])
    services.plan_barcodes(tp_experiment, seed=1)
    probs = " ".join(services.validate_plan(tp_experiment))
    assert "does not exist in the plate format" in probs and "more than one plate format" in probs
    with pytest.raises(PlannerError, match="unknown plate format"):
        services.upload_samples(tp_experiment, [{"sample_id": "x", "plate_format": "384"}], users["tech"])


def test_snapshot_unchanged_for_default_plans(tp_experiment, users):
    """Approvals made before v0.4 must keep their fingerprint."""
    services.upload_samples(tp_experiment, sample_rows(10), users["tech"])
    services.plan_barcodes(tp_experiment, seed=1)
    snap = services.plan_snapshot(tp_experiment)
    assert "plate_formats" not in snap and "library_layout" not in snap


def test_pcr_layout_matches_star_simulation(tp_experiment, users):
    sim = Path(__file__).resolve().parents[2] / "tag_and_pool_hamilton_star_sim" / "src"
    if not sim.exists():
        pytest.skip("simulation repo not checked out next to this repo")
    pd = pytest.importorskip("pandas")
    sys.path.insert(0, str(sim))
    from libprep.samplesheet import plan_layout

    services.upload_samples(tp_experiment, sample_rows(200), users["tech"])
    services.plan_barcodes(tp_experiment, pool_sizes=[24, 17, 9, 24, 24, 3, 24, 24, 24, 24, 3], seed=3)
    rng = random.Random(4)  # a few manual swaps between pools, like drag & drop would do
    smp = list(tp_experiment.samples.all())
    for _ in range(15):
        a, b = rng.sample(smp, 2)
        services.move_sample(a, target_sample=b)
    samples = list(tp_experiment.samples.select_related("pool", "well_barcode"))
    ours = plates.pcr_layout([{"sample_id": s.sample_id, "pool_id": s.pool.pool_id, "source_plate": s.source_plate,
                               "source_well": s.source_well, "plate_format": s.plate_format} for s in samples])
    df = pd.DataFrame([{"sample_id": s.sample_id, "source_plate": s.source_plate, "source_well": s.source_well,
                        "condition": s.condition, "pool_id": s.pool.pool_id, "barcode_id": s.well_barcode.barcode_id}
                       for s in samples])
    bc = pd.DataFrame([{"barcode_id": b.barcode_id, "well": b.well} for b in tp_experiment.well_barcode_set.barcodes.all()])
    theirs = plan_layout(df, bc).samples.set_index("sample_id")
    for sid, pos in ours.items():
        assert (pos.plate, pos.well) == (theirs.loc[sid, "pcr_plate"], theirs.loc[sid, "pcr_well"]), sid


# --- library structure --------------------------------------------------------
def test_library_default_and_well_barcode_check():
    an = library.analyse(library.default_layout("tag_and_pool"), 28, 90, 8, 8, 300, ("R2", 1, 8))
    r2 = next(r for r in an.reads if r.name == "Read 2")
    assert r2.covers[0]["type"] == "well_bc" and (r2.covers[0]["first"], r2.covers[0]["last"]) == (1, 8)
    assert ("ok", "Well barcode = R2 bases 1–8, as in the barcode map.") in an.messages
    lay = library.default_layout("tag_and_pool")
    lay.insert(lay.index(next(s for s in lay if s["type"] == "read2_primer")), {"type": "linker", "label": "L", "length": 4})
    an = library.analyse(lay, 28, 90, 8, 8, 300, ("R2", 1, 8))
    assert any(lvl == "error" and "R2 5–12" in t for lvl, t in an.messages)
    assert "<svg" in library.svg(an)


def test_library_structure_errors():
    an = library.analyse([{"type": "p5", "label": "P5", "length": 29}], 28, 90, 8, 8, None)
    assert sum(lvl == "error" for lvl, _ in an.messages) == 2  # no read 1 / read 2 primer sites
    with pytest.raises(ValueError):
        library.clean_layout([{"type": "nonsense"}])


def test_library_layout_locked_with_plan(tp_experiment, users, flowcell):
    services.save_library_layout(tp_experiment, library.default_layout("tag_and_pool"))
    services.upload_samples(tp_experiment, sample_rows(10), users["tech"])
    services.plan_barcodes(tp_experiment, seed=1)
    services.accept_experiment(tp_experiment, users["bioinf"])
    assert "library_layout" in services.plan_snapshot(tp_experiment)  # covered by the final approval
    run = SequencingRun.objects.create(run_id="NGS-LIB", flowcell_type=flowcell)
    services.add_experiment_to_run(run, tp_experiment)
    tp_experiment.refresh_from_db()
    with pytest.raises(PlannerError, match="locked"):
        services.save_library_layout(tp_experiment, [])


# --- drag & drop: barcodes and pools -----------------------------------------
def _planned(tp_experiment, users, n=30, sizes=None):
    services.upload_samples(tp_experiment, sample_rows(n), users["tech"])
    services.plan_barcodes(tp_experiment, pool_sizes=sizes, seed=1)
    return {s.sample_id: s for s in tp_experiment.samples.select_related("pool", "well_barcode")}


def test_assign_barcode_swaps_within_pool(tp_experiment, users):
    s = _planned(tp_experiment, users)
    a, b = s["S001"], next(x for x in s.values() if x.pool_id == s["S001"].pool_id and x.pk != s["S001"].pk)
    old_a, old_b = a.well_barcode, b.well_barcode
    services.assign_barcode(a, old_b)
    a.refresh_from_db(); b.refresh_from_db(); tp_experiment.refresh_from_db()
    assert (a.well_barcode, b.well_barcode) == (old_b, old_a)
    assert tp_experiment.planning_seed is None  # marked as edited by hand
    assert services.validate_plan(tp_experiment) == []


def test_move_sample_between_pools(tp_experiment, users):
    s = _planned(tp_experiment, users, n=34, sizes=[24, 10])
    p1, p2 = list(tp_experiment.pools.order_by("pool_id"))
    small = next(x for x in s.values() if x.pool_id == p2.pk)
    big = next(x for x in s.values() if x.pool_id == p1.pk)
    with pytest.raises(PlannerError, match="full"):
        services.move_sample(small, target_pool=p1)  # pool 1 has all 24 barcodes in use
    services.move_sample(big, target_pool=p2)  # takes a free barcode in pool 2
    services.move_sample(small, target_sample=s[next(k for k, v in s.items() if v.pool_id == p1.pk and k != big.sample_id)])
    services.move_sample(big, new_pool=True)
    assert tp_experiment.pools.count() == 3
    assert services.validate_plan(tp_experiment) == []
    lone = tp_experiment.pools.order_by("-pool_id").first()
    services.move_sample(lone.samples.first(), target_pool=p2)
    assert tp_experiment.pools.count() == 2  # the emptied pool is removed


def test_barcode_edits_locked_in_run(tp_experiment, users, flowcell):
    s = _planned(tp_experiment, users)
    services.accept_experiment(tp_experiment, users["bioinf"])
    run = SequencingRun.objects.create(run_id="NGS-DL", flowcell_type=flowcell)
    services.add_experiment_to_run(run, tp_experiment)
    a = tp_experiment.samples.first()
    with pytest.raises(PlannerError, match="locked"):
        services.move_sample(a, new_pool=True)


# --- drag & drop: index grid ---------------------------------------------------
def test_index_set_swap_clear_and_lock(tp_experiment, users, index_set, flowcell):
    _planned(tp_experiment, users, n=48)
    services.accept_experiment(tp_experiment, users["bioinf"])
    run = SequencingRun.objects.create(run_id="NGS-IX", flowcell_type=flowcell)
    services.add_experiment_to_run(run, tp_experiment)
    services.assign_indexes(run, index_set)
    p1, p2 = list(tp_experiment.pools.order_by("pool_id"))
    pair1, pair2 = (p1.i7, p1.i5), (p2.i7, p2.i5)
    services.set_pool_index(run, p1, *pair2)  # occupied → swap
    p1.refresh_from_db(); p2.refresh_from_db()
    assert (p1.i7, p1.i5, p2.i7, p2.i5) == (*pair2, *pair1)
    services.clear_pool_index(run, p1)
    p1.refresh_from_db()
    assert p1.i7 is None
    services.set_pool_index(run, p1, *pair2)  # free again after clearing
    services.decide_run(services.submit_run(run, users["bioinf"]), users["alex"], approve=True)
    p1.refresh_from_db()
    with pytest.raises(PlannerError, match="locked"):
        services.clear_pool_index(run, p1)
    # an amendment unlocks only the amended experiment's pools
    tp_experiment.refresh_from_db()
    services.reopen_experiment(tp_experiment, users["tech"], "swap indexes")
    p1.refresh_from_db()
    services.clear_pool_index(run, p1)
    assert p1 in services.index_scope(run)


# --- endpoints and pages ---------------------------------------------------------
def test_dnd_endpoints_and_pages(client, users, tp_experiment, index_set, flowcell):
    s = _planned(tp_experiment, users)
    client.force_login(users["tech"])
    for name in ("experiment_plates", "experiment_library"):
        r = client.get(reverse(name, args=[tp_experiment.pk]))
        assert r.status_code == 200 and "Drag" in r.content.decode()
    a = s["S001"]
    other_bc = tp_experiment.well_barcode_set.barcodes.exclude(pk=a.well_barcode_id).first()
    url = reverse("experiment_dnd", args=[tp_experiment.pk])
    r = client.post(url, json.dumps({"op": "assign_barcode", "sample": a.pk, "barcode": other_bc.pk}), content_type="application/json")
    assert r.json()["ok"] and other_bc.barcode_id in r.json()["message"]
    r = client.post(url, json.dumps({"op": "move_sample", "sample": a.pk, "pool": "new"}), content_type="application/json")
    assert r.json()["ok"]
    r = client.post(url, json.dumps({"op": "bogus"}), content_type="application/json")
    assert r.status_code == 400 and not r.json()["ok"]

    lib = reverse("experiment_library_api", args=[tp_experiment.pk, "preview"])
    r = client.post(lib, json.dumps({"layout": library.default_layout("tag_and_pool")}), content_type="application/json")
    assert "<svg" in r.json()["html"]
    r = client.post(lib.replace("preview", "template"), json.dumps({"layout": library.default_layout("tag_and_pool")[:-1]}),
                    content_type="application/json")
    assert r.json()["ok"] and services.library_template("tag_and_pool")[-1]["type"] == "i7"

    services.accept_experiment(tp_experiment, users["bioinf"])
    run = SequencingRun.objects.create(run_id="NGS-W", flowcell_type=flowcell)
    services.add_experiment_to_run(run, tp_experiment)
    page = client.get(reverse("run_indexes", args=[run.pk])).content.decode()
    assert "i7_01" in page and "Pools without index" in page
    pool = tp_experiment.pools.first()
    i7 = index_set.primers.filter(kind="i7").first()
    i5 = index_set.primers.filter(kind="i5").first()
    r = client.post(reverse("run_dnd", args=[run.pk]), json.dumps({"op": "set_index", "pool": pool.pk, "i7": i7.pk, "i5": i5.pk}),
                    content_type="application/json")
    assert r.json()["ok"]
    pool.refresh_from_db()
    assert (pool.i7, pool.i5) == (i7, i5)
