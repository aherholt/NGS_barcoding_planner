"""Data model of the NGS barcoding planner.

How the pieces fit together (read top to bottom):

    Experiment            one row of the SharePoint intake list
      └─ Sample           one lysate / one biological sample
           └─ Pool        Tag&Pool: up to N samples pooled after well-barcode annealing
                          other library types: one indexed library = one "pool"
                └─ i7 + i5 sample index (IndexPrimer)
    SequencingRun         one flow cell at the provider
      └─ RunPool          which pool goes into which run, with its read target

Reference data (versioned, never edited after use): WellBarcodeSet/WellBarcode,
IndexSet/IndexPrimer, FlowcellType.

Audit trail: every model registered with `HistoricalRecords()` keeps a full copy
of each version with user and timestamp. SignOff and Deviation are append-only.
"""

from django.conf import settings
from django.db import models
from django.urls import reverse
from simple_history.models import HistoricalRecords

DNA = "ACGT"


def clean_seq(seq: str) -> str:
    return (seq or "").strip().upper().replace(" ", "")


# --------------------------------------------------------------------------- #
# Reference data
# --------------------------------------------------------------------------- #
class WellBarcodeSet(models.Model):
    """A set of well-barcode (cDNA/RT) primers, e.g. 'WBC-24 v1'. Extend by creating a NEW set."""

    name = models.CharField(max_length=80, unique=True)
    description = models.TextField(blank=True)
    created = models.DateTimeField(auto_now_add=True)
    history = HistoricalRecords()

    def __str__(self):
        return self.name

    @property
    def size(self) -> int:
        return self.barcodes.count()


class WellBarcode(models.Model):
    barcode_set = models.ForeignKey(WellBarcodeSet, on_delete=models.PROTECT, related_name="barcodes")
    barcode_id = models.CharField(max_length=40, help_text="e.g. BC01 — must match the barcode stock plate")
    well = models.CharField(max_length=4, help_text="position in the 96-well barcode stock plate, e.g. A1")
    sequence = models.CharField(max_length=40)
    history = HistoricalRecords()

    class Meta:
        ordering = ["barcode_set", "barcode_id"]
        constraints = [
            models.UniqueConstraint(fields=["barcode_set", "barcode_id"], name="uniq_wbc_id"),
            models.UniqueConstraint(fields=["barcode_set", "well"], name="uniq_wbc_well"),
        ]

    def save(self, *args, **kwargs):
        self.sequence = clean_seq(self.sequence)
        self.well = self.well.strip().upper()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.barcode_id} ({self.sequence})"


class IndexSet(models.Model):
    """A set of i7/i5 sample-index primers (custom combinatorial dual-index set)."""

    name = models.CharField(max_length=80, unique=True)
    description = models.TextField(blank=True)
    history = HistoricalRecords()

    def __str__(self):
        return self.name


class IndexPrimer(models.Model):
    class Kind(models.TextChoices):
        I7 = "i7", "i7 (Index 1)"
        I5 = "i5", "i5 (Index 2)"

    index_set = models.ForeignKey(IndexSet, on_delete=models.PROTECT, related_name="primers")
    kind = models.CharField(max_length=2, choices=Kind.choices)
    primer_id = models.CharField(max_length=40)
    sequence = models.CharField(
        max_length=40,
        help_text="i5: enter in the orientation stored in your primer sheet; the sample sheet export can reverse-complement it.",
    )
    well = models.CharField(max_length=4, blank=True)
    history = HistoricalRecords()

    class Meta:
        ordering = ["index_set", "kind", "primer_id"]
        constraints = [models.UniqueConstraint(fields=["index_set", "kind", "primer_id"], name="uniq_index_primer")]

    def save(self, *args, **kwargs):
        self.sequence = clean_seq(self.sequence)
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.primer_id} ({self.sequence})"


class FlowcellType(models.Model):
    """Sequencing capacity options. Enter your PROVIDER's guaranteed output, not Illumina's spec sheet."""

    name = models.CharField(max_length=80, unique=True)
    output_m_read_pairs = models.FloatField(help_text="usable output in million read pairs (clusters)")
    price_eur = models.FloatField(null=True, blank=True)
    notes = models.TextField(blank=True)
    active = models.BooleanField(default=True)
    history = HistoricalRecords()

    class Meta:
        ordering = ["output_m_read_pairs"]

    def __str__(self):
        return f"{self.name} ({self.output_m_read_pairs:,.0f} M)"


class LibraryTemplate(models.Model):
    """Default library structure per library type (used for new experiments)."""

    library_type = models.CharField(max_length=20, unique=True)
    layout = models.JSONField(default=list)
    updated = models.DateTimeField(auto_now=True)
    history = HistoricalRecords()

    def __str__(self):
        return f"Template {self.library_type}"


# --------------------------------------------------------------------------- #
# Experiments
# --------------------------------------------------------------------------- #
class Experiment(models.Model):
    class LibraryType(models.TextChoices):
        TAG_AND_POOL = "tag_and_pool", "Tag&Pool (well barcodes)"
        BULK_RNASEQ = "bulk_rnaseq", "Bulk RNA-seq"
        SN_RNASEQ = "sn_rnaseq", "Single-nucleus RNA-seq"
        CRISPR_SCREEN = "crispr_screen", "Pooled CRISPR screen (sgRNA/barcode library)"
        OTHER = "other", "Other"

    class Complexity(models.TextChoices):
        NORMAL = "normal", "Normal / high"
        LOW = "low", "Low (amplicon, sgRNA, barcode)"

    class Status(models.TextChoices):
        # Order of the workflow: everything is planned and approved BEFORE the lab work starts.
        SUBMITTED = "submitted", "Submitted"
        ACCEPTED = "accepted", "Accepted – in planning"
        IN_RUN = "in_run", "Assigned to NGS run"
        PLAN_IN_REVIEW = "plan_in_review", "Final plan in review"
        PLAN_APPROVED = "plan_approved", "Plan approved – ready for library prep"
        AMENDING = "amending", "Plan reopened – amendment"
        LIBPREP_DONE = "libprep_done", "Library prep done"
        SUBMITTED_TO_PROVIDER = "submitted_to_provider", "Submitted to provider"
        DATA_DELIVERED = "data_delivered", "Data delivered"
        ON_HOLD = "on_hold", "On hold"
        CANCELLED = "cancelled", "Cancelled"

    # Samples, pools and well barcodes can only be changed while the experiment is NOT in a run.
    # To change a plan that is in a run: remove the experiment from the run (only while the run is in planning).
    PLAN_LOCKED_STATUSES = {
        Status.IN_RUN, Status.PLAN_IN_REVIEW, Status.PLAN_APPROVED, Status.LIBPREP_DONE,
        Status.SUBMITTED_TO_PROVIDER, Status.DATA_DELIVERED, Status.CANCELLED,
    }

    code = models.SlugField(max_length=30, unique=True, help_text="short ID used in pool and sample-sheet names, e.g. TP26-014")
    title = models.CharField(max_length=200)
    sharepoint_item_id = models.IntegerField(null=True, blank=True, unique=True)
    library_type = models.CharField(max_length=20, choices=LibraryType.choices, default=LibraryType.TAG_AND_POOL)
    description = models.TextField(blank=True)

    responsible_assay = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    responsible_libprep = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    responsible_ngs = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    eln_assay_url = models.URLField(blank=True)
    eln_libprep_url = models.URLField(blank=True)

    lysis_date = models.DateField(null=True, blank=True)
    delivery_deadline = models.DateField(null=True, blank=True)

    r1_length = models.PositiveIntegerField(null=True, blank=True)
    r2_length = models.PositiveIntegerField(null=True, blank=True)
    i7_length = models.PositiveIntegerField(null=True, blank=True)
    i5_length = models.PositiveIntegerField(null=True, blank=True)
    requested_m_read_pairs = models.FloatField(null=True, blank=True, help_text="total for the experiment, million read pairs")
    library_complexity = models.CharField(max_length=10, choices=Complexity.choices, default=Complexity.NORMAL)
    avg_product_length = models.PositiveIntegerField(null=True, blank=True)
    avg_insert_length = models.PositiveIntegerField(null=True, blank=True)

    well_barcode_set = models.ForeignKey(WellBarcodeSet, null=True, blank=True, on_delete=models.PROTECT)
    amendment_run = models.ForeignKey(
        "SequencingRun", null=True, blank=True, on_delete=models.PROTECT, related_name="amendments",
        help_text="set while the plan of this experiment is reopened inside an approved run (amendment)")
    planning_seed = models.IntegerField(null=True, blank=True, help_text="random seed of the last automatic barcode layout "
                                                                          "(empty after manual drag & drop changes)")
    library_layout = models.JSONField(default=list, blank=True,
                                      help_text="library structure P5 → P7: list of {type, label, length}")

    status = models.CharField(max_length=30, choices=Status.choices, default=Status.SUBMITTED)
    sharepoint_push_pending = models.BooleanField(default=False)
    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)
    history = HistoricalRecords()

    class Meta:
        ordering = ["-created"]

    def __str__(self):
        return f"{self.code} – {self.title}"

    def get_absolute_url(self):
        return reverse("experiment_detail", args=[self.pk])

    @property
    def is_tag_and_pool(self) -> bool:
        return self.library_type == self.LibraryType.TAG_AND_POOL

    @property
    def plan_locked(self) -> bool:
        return self.status in self.PLAN_LOCKED_STATUSES

    @property
    def read_structure(self) -> str:
        parts = [self.r1_length, self.i7_length, self.i5_length, self.r2_length]
        if any(p is None for p in parts):
            return "incomplete"
        return f"{self.r1_length}-{self.i7_length}-{self.i5_length}-{self.r2_length}"


class Pool(models.Model):
    """Tag&Pool: one tube of pooled, well-barcoded samples. Other types: one indexed library."""

    experiment = models.ForeignKey(Experiment, on_delete=models.CASCADE, related_name="pools")
    pool_id = models.CharField(max_length=60, help_text="unique within the experiment; used as Sample_ID in the sample sheet")
    i7 = models.ForeignKey(IndexPrimer, null=True, blank=True, on_delete=models.PROTECT, related_name="+", limit_choices_to={"kind": "i7"})
    i5 = models.ForeignKey(IndexPrimer, null=True, blank=True, on_delete=models.PROTECT, related_name="+", limit_choices_to={"kind": "i5"})
    notes = models.TextField(blank=True)
    history = HistoricalRecords()

    class Meta:
        ordering = ["experiment", "pool_id"]
        constraints = [models.UniqueConstraint(fields=["experiment", "pool_id"], name="uniq_pool")]

    def __str__(self):
        return self.pool_id


class Sample(models.Model):
    experiment = models.ForeignKey(Experiment, on_delete=models.CASCADE, related_name="samples")
    sample_id = models.CharField(max_length=80)
    source_plate = models.CharField(max_length=40, blank=True, help_text="culture plate ID, e.g. CULT01")
    source_well = models.CharField(max_length=4, blank=True, help_text="e.g. A1..D6 for 24-well plates")
    plate_format = models.PositiveSmallIntegerField(default=24, help_text="culture plate format: 6, 12, 24, 48 or 96 wells")
    condition = models.CharField(max_length=200, blank=True)
    pool = models.ForeignKey(Pool, null=True, blank=True, on_delete=models.SET_NULL, related_name="samples")
    well_barcode = models.ForeignKey(WellBarcode, null=True, blank=True, on_delete=models.PROTECT)
    order = models.PositiveIntegerField(default=0, help_text="row order of the uploaded sample list")
    history = HistoricalRecords()

    class Meta:
        ordering = ["experiment", "order", "sample_id"]
        constraints = [models.UniqueConstraint(fields=["experiment", "sample_id"], name="uniq_sample")]

    def __str__(self):
        return self.sample_id


# --------------------------------------------------------------------------- #
# Sequencing runs
# --------------------------------------------------------------------------- #
class SequencingRun(models.Model):
    class Status(models.TextChoices):
        PLANNING = "planning", "Planning"
        IN_REVIEW = "in_review", "Final plan in review"
        APPROVED = "approved", "Approved – library prep"
        SUBMITTED = "submitted", "Submitted to provider"
        DATA_DELIVERED = "data_delivered", "Data delivered"
        CANCELLED = "cancelled", "Cancelled"

    LOCKED_STATUSES = {Status.IN_REVIEW, Status.APPROVED, Status.SUBMITTED, Status.DATA_DELIVERED}

    run_id = models.SlugField(max_length=40, unique=True, help_text="e.g. NGS26-007 — written back to SharePoint")
    provider = models.CharField(max_length=100, blank=True)
    flowcell_type = models.ForeignKey(FlowcellType, null=True, blank=True, on_delete=models.PROTECT)
    planned_submission_date = models.DateField(null=True, blank=True)
    r1_length = models.PositiveIntegerField(null=True, blank=True)
    r2_length = models.PositiveIntegerField(null=True, blank=True)
    i7_length = models.PositiveIntegerField(null=True, blank=True)
    i5_length = models.PositiveIntegerField(null=True, blank=True)
    phix_percent = models.FloatField(null=True, blank=True, help_text="empty = automatic (more PhiX if a low-complexity library is present)")
    safety_margin_percent = models.FloatField(null=True, blank=True, help_text="empty = default from settings")
    i5_reverse_complement = models.BooleanField(
        default=False,
        help_text="Reverse-complement i5 in the sample sheet export. Ask your provider which orientation they expect.",
    )
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PLANNING)
    notes = models.TextField(blank=True)
    created = models.DateTimeField(auto_now_add=True)
    history = HistoricalRecords()

    class Meta:
        ordering = ["-created"]

    def __str__(self):
        return self.run_id

    def get_absolute_url(self):
        return reverse("run_detail", args=[self.pk])

    @property
    def locked(self) -> bool:
        return self.status in self.LOCKED_STATUSES

    @property
    def read_structure(self) -> str:
        parts = [self.r1_length, self.i7_length, self.i5_length, self.r2_length]
        if any(p is None for p in parts):
            return "not set"
        return f"{self.r1_length}-{self.i7_length}-{self.i5_length}-{self.r2_length}"

    def experiments(self):
        return Experiment.objects.filter(pools__run_links__run=self).distinct()


class RunPool(models.Model):
    run = models.ForeignKey(SequencingRun, on_delete=models.CASCADE, related_name="run_pools")
    pool = models.ForeignKey(Pool, on_delete=models.PROTECT, related_name="run_links")
    target_m_read_pairs = models.FloatField(default=0)
    history = HistoricalRecords()

    class Meta:
        ordering = ["run", "pool__experiment", "pool__pool_id"]
        constraints = [models.UniqueConstraint(fields=["run", "pool"], name="uniq_run_pool")]

    def __str__(self):
        return f"{self.run} / {self.pool}"


# --------------------------------------------------------------------------- #
# Sign-offs and deviations (append-only)
# --------------------------------------------------------------------------- #
class SignOff(models.Model):
    class Step(models.TextChoices):
        ACCEPT = "accept", "Experiment accepted"
        BARCODE_PLAN = "barcode_plan", "Barcode plan (old workflow, v0.1)"
        RUN_PLAN = "run_plan", "Final plan (well barcodes + sample indexes)"
        AMENDMENT = "amendment", "Plan amendment (one experiment, re-checked across the run)"
        LIBPREP = "libprep", "Library prep executed"
        DATA_DELIVERED = "data_delivered", "Data delivered"

    class State(models.TextChoices):
        PENDING = "pending", "Waiting for approval"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"
        SUPERSEDED = "superseded", "Superseded (plan reopened)"

    experiment = models.ForeignKey(Experiment, null=True, blank=True, on_delete=models.PROTECT, related_name="signoffs")
    run = models.ForeignKey(SequencingRun, null=True, blank=True, on_delete=models.PROTECT, related_name="signoffs")
    step = models.CharField(max_length=20, choices=Step.choices)
    state = models.CharField(max_length=12, choices=State.choices, default=State.PENDING)
    submitted_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    submitted_at = models.DateTimeField(auto_now_add=True)
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    decided_at = models.DateTimeField(null=True, blank=True)
    comment = models.TextField(blank=True)
    snapshot = models.JSONField(default=dict, help_text="frozen copy of the plan at submission")
    snapshot_hash = models.CharField(max_length=64, blank=True)

    class Meta:
        ordering = ["-submitted_at"]

    def __str__(self):
        target = self.experiment or self.run
        return f"{self.get_step_display()} – {target} – {self.get_state_display()}"


class Deviation(models.Model):
    """Anything that did not go as planned, or a reason for reopening an approved plan."""

    experiment = models.ForeignKey(Experiment, null=True, blank=True, on_delete=models.PROTECT, related_name="deviations")
    run = models.ForeignKey(SequencingRun, null=True, blank=True, on_delete=models.PROTECT, related_name="deviations")
    description = models.TextField()
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.created_at:%Y-%m-%d} {self.description[:60]}"
