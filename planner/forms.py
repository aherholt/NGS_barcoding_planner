from django import forms

from .models import Experiment, IndexSet, SequencingRun

DATE = forms.DateInput(attrs={"type": "date"})

EXPERIMENT_FIELDS = [
    "code", "title", "library_type", "description", "responsible_assay", "responsible_libprep", "responsible_ngs",
    "eln_assay_url", "eln_libprep_url", "lysis_date", "delivery_deadline",
    "r1_length", "r2_length", "i7_length", "i5_length", "requested_m_read_pairs", "library_complexity",
    "avg_product_length", "avg_insert_length", "well_barcode_set",
]
# fields that may still change once the barcode plan is locked
UNLOCKED_FIELDS = ["title", "description", "responsible_assay", "responsible_libprep", "responsible_ngs",
                   "eln_assay_url", "eln_libprep_url", "delivery_deadline"]


class ExperimentForm(forms.ModelForm):
    class Meta:
        model = Experiment
        fields = EXPERIMENT_FIELDS
        widgets = {"lysis_date": DATE, "delivery_deadline": DATE, "description": forms.Textarea(attrs={"rows": 3})}
        labels = {"requested_m_read_pairs": "Requested output (M read pairs, total)"}


class LockedExperimentForm(ExperimentForm):
    class Meta(ExperimentForm.Meta):
        fields = UNLOCKED_FIELDS


class UploadForm(forms.Form):
    file = forms.FileField(help_text="CSV or Excel. Columns: sample_id (required), source_plate, source_well, "
                                     "plate_format (6/12/24/48/96, default 24), condition; "
                                     "optional pool_id + barcode_id to import a ready-made plan.")


class PlanForm(forms.Form):
    max_pool_size = forms.IntegerField(required=False, min_value=1, help_text="even split; empty = barcode set size")
    pool_sizes = forms.CharField(required=False, help_text="or explicit sizes, e.g. 24,24,12 (overrides max pool size)")
    shuffle = forms.BooleanField(required=False, initial=True, help_text="different random barcode order per pool")
    seed = forms.IntegerField(required=False, help_text="empty = new random seed; enter an old seed to reproduce a layout")

    def clean_pool_sizes(self):
        raw = self.cleaned_data["pool_sizes"].strip()
        if not raw:
            return None
        try:
            return [int(x) for x in raw.replace(";", ",").split(",") if x.strip()]
        except ValueError:
            raise forms.ValidationError("Use whole numbers separated by commas.")


class CommentForm(forms.Form):
    comment = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}))


class ReasonForm(forms.Form):
    reason = forms.CharField(widget=forms.Textarea(attrs={"rows": 2}), help_text="stored permanently in the audit trail")


class LibprepForm(forms.Form):
    comment = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}))
    manifest = forms.FileField(required=False, help_text="optional: pool_manifest.csv from the STAR run — compared with the plan")


class DeviationForm(forms.Form):
    description = forms.CharField(widget=forms.Textarea(attrs={"rows": 3}))


class RunForm(forms.ModelForm):
    class Meta:
        model = SequencingRun
        fields = ["run_id", "provider", "flowcell_type", "planned_submission_date", "r1_length", "i7_length",
                  "i5_length", "r2_length", "phix_percent", "safety_margin_percent", "i5_reverse_complement", "notes"]
        widgets = {"planned_submission_date": DATE, "notes": forms.Textarea(attrs={"rows": 2})}
        labels = {"r1_length": "R1 cycles", "r2_length": "R2 cycles", "i7_length": "i7 cycles", "i5_length": "i5 cycles"}


class AddExperimentForm(forms.Form):
    experiment = forms.ModelChoiceField(queryset=Experiment.objects.none())

    def __init__(self, *args, run=None, **kwargs):
        super().__init__(*args, **kwargs)
        qs = Experiment.objects.filter(status=Experiment.Status.ACCEPTED)  # planned, not yet in a run
        if run is not None:
            qs = qs.exclude(pools__run_links__run=run)
        self.fields["experiment"].queryset = qs.distinct().order_by("code")


class AssignIndexForm(forms.Form):
    index_set = forms.ModelChoiceField(queryset=IndexSet.objects.all())
    overwrite = forms.BooleanField(required=False, help_text="re-assign indexes that are already set in this run")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        sets = list(self.fields["index_set"].queryset[:2])
        if len(sets) == 1:
            self.fields["index_set"].initial = sets[0].pk
