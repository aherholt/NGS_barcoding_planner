"""Import a well-barcode set or an index-primer set from CSV/Excel.

Examples (PowerShell):
  python manage.py import_reference well-barcodes "WBC-24 v1" .\\data\\well_barcodes.xlsx
  python manage.py import_reference indexes "Systasy UDI v1" .\\data\\index_primers.csv
"""

from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from planner.reference_import import import_index_primers, import_well_barcodes
from planner.services import PlannerError
from planner.tabular import read_table


class Command(BaseCommand):
    help = "Import well barcodes (barcode_id, well, sequence) or index primers (kind, primer_id, sequence, well)."

    def add_arguments(self, parser):
        parser.add_argument("kind", choices=["well-barcodes", "indexes"])
        parser.add_argument("set_name")
        parser.add_argument("file")
        parser.add_argument("--sheet", help="Excel sheet name (default: first sheet)")
        parser.add_argument("--description", default="")

    def handle(self, kind, set_name, file, sheet=None, description="", **_):
        path = Path(file)
        if not path.exists():
            raise CommandError(f"File not found: {path}")
        rows = read_table(path.read_bytes(), path.name, sheet)
        try:
            if kind == "well-barcodes":
                s = import_well_barcodes(set_name, rows, description)
                self.stdout.write(self.style.SUCCESS(f"Imported {s.barcodes.count()} well barcodes into '{s}'."))
            else:
                s = import_index_primers(set_name, rows, description)
                n7, n5 = s.primers.filter(kind="i7").count(), s.primers.filter(kind="i5").count()
                self.stdout.write(self.style.SUCCESS(f"Imported {n7} i7 and {n5} i5 primers into '{s}' ({n7 * n5} combinations)."))
        except PlannerError as e:
            raise CommandError("\n  - " + "\n  - ".join(e.problems))
