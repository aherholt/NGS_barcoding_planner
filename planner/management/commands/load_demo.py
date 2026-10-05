"""Create demo data so you can click through the app without real sequences.

  python manage.py load_demo

Creates: users tech/bioinf/alex (password 'demo-pass-123'), a 24-barcode set with
PLACEHOLDER-like random sequences, a 12×18 index set, three flow cell types and
two experiments. NEVER run this on the production database.
"""

import random

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from planner.models import Experiment, FlowcellType, WellBarcodeSet
from planner.reference_import import import_index_primers, import_well_barcodes

WELLS = [f"{r}{c}" for c in range(1, 13) for r in "ABCDEFGH"]


def rand_seqs(n, length, rng, min_dist=3):
    out = []
    while len(out) < n:
        s = "".join(rng.choice("ACGT") for _ in range(length))
        if all(sum(a != b for a, b in zip(s, o)) >= min_dist for o in out):
            out.append(s)
    return out


class Command(BaseCommand):
    help = "Load demo data (development only)."

    def handle(self, **_):
        if WellBarcodeSet.objects.filter(name="DEMO WBC-24").exists():
            raise CommandError("Demo data already loaded.")
        rng = random.Random(42)
        User = get_user_model()
        for name, first in (("tech", "Tina Tech"), ("bioinf", "Ben Bioinf"), ("alex", "Alex")):
            u, _ = User.objects.get_or_create(username=name, defaults={"email": f"{name}@example.org", "first_name": first})
            u.set_password("demo-pass-123")
            u.is_staff = u.is_superuser = name == "alex"
            u.save()
        bcs = rand_seqs(24, 8, rng)
        wset = import_well_barcodes("DEMO WBC-24", [
            {"barcode_id": f"BC{i + 1:02d}", "well": WELLS[i], "sequence": s} for i, s in enumerate(bcs)], "demo only")
        i7 = rand_seqs(12, 8, rng)
        i5 = rand_seqs(18, 8, rng)
        iset = import_index_primers("DEMO index set", [
            *({"kind": "i7", "primer_id": f"i7_{i + 1:02d}", "sequence": s} for i, s in enumerate(i7)),
            *({"kind": "i5", "primer_id": f"i5_{i + 1:02d}", "sequence": s} for i, s in enumerate(i5)),
        ], "demo only")
        for name, out in (("NovaSeq X 1.5B (placeholder)", 1500), ("NovaSeq X 10B (placeholder)", 10000),
                          ("NovaSeq X 25B (placeholder)", 25000)):
            FlowcellType.objects.get_or_create(name=name, defaults={"output_m_read_pairs": out,
                                                                    "notes": "Replace with your provider's guaranteed output."})
        tech, bioinf = User.objects.get(username="tech"), User.objects.get(username="bioinf")
        Experiment.objects.create(code="TP26-001", title="Compound screen iNeurons (demo)", library_type="tag_and_pool",
                                  responsible_libprep=tech, responsible_ngs=bioinf, well_barcode_set=wset,
                                  r1_length=28, r2_length=90, i7_length=8, i5_length=8, requested_m_read_pairs=400)
        Experiment.objects.create(code="CR26-002", title="CRISPRi screen sgRNA counts (demo)", library_type="crispr_screen",
                                  library_complexity="low", responsible_libprep=tech, responsible_ngs=bioinf,
                                  r1_length=28, r2_length=90, i7_length=8, i5_length=8, requested_m_read_pairs=200)
        self.stdout.write(self.style.SUCCESS(f"Demo data loaded: {wset}, {iset}, 3 flow cells, 2 experiments. "
                                             "Logins: tech / bioinf / alex, password demo-pass-123"))
