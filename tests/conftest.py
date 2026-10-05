import random

import pytest
from django.contrib.auth import get_user_model

from planner.models import Experiment, FlowcellType
from planner.reference_import import import_index_primers, import_well_barcodes

WELLS96 = [f"{r}{c}" for c in range(1, 13) for r in "ABCDEFGH"]
WELLS24 = [f"{r}{c}" for c in range(1, 7) for r in "ABCD"]


def _seqs(n, rng, length=8, min_dist=3):
    out = []
    while len(out) < n:
        s = "".join(rng.choice("ACGT") for _ in range(length))
        if all(sum(a != b for a, b in zip(s, o)) >= min_dist for o in out):
            out.append(s)
    return out


@pytest.fixture
def users(db):
    User = get_user_model()
    return {n: User.objects.create_user(n, f"{n}@example.org", "pw") for n in ("tech", "bioinf", "alex")}


@pytest.fixture
def wbc_set(db):
    rng = random.Random(1)
    return import_well_barcodes("WBC-24 test", [
        {"barcode_id": f"BC{i + 1:02d}", "well": WELLS96[i], "sequence": s} for i, s in enumerate(_seqs(24, rng))])


@pytest.fixture
def index_set(db):
    rng = random.Random(2)
    seqs = _seqs(30, rng)
    return import_index_primers("IDX test", [
        *({"kind": "i7", "primer_id": f"i7_{i + 1:02d}", "sequence": s} for i, s in enumerate(seqs[:12])),
        *({"kind": "i5", "primer_id": f"i5_{i + 1:02d}", "sequence": s} for i, s in enumerate(seqs[12:]))])


@pytest.fixture
def flowcell(db):
    return FlowcellType.objects.create(name="10B test", output_m_read_pairs=10000)


@pytest.fixture
def tp_experiment(db, wbc_set):
    return Experiment.objects.create(code="TP-1", title="tag and pool", library_type="tag_and_pool", well_barcode_set=wbc_set,
                                     r1_length=28, r2_length=90, i7_length=8, i5_length=8, requested_m_read_pairs=600)


def sample_rows(n, plates=None):
    rows = []
    for i in range(n):
        plate, well = divmod(i, 24)
        rows.append({"sample_id": f"S{i + 1:03d}", "source_plate": f"CULT{plate + 1:02d}",
                     "source_well": WELLS24[well], "condition": "DMSO" if i % 2 else "drug"})
    return rows
