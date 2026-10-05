"""Unit tests for the run checks (no database needed)."""

from django.conf import settings

from planner.checks import Library, RunSpec, check_run, flowcell_fit, hamming, required_output

CFG = settings.PLANNER


def lib(name, i7, i5, target=10.0, exp="E1", **kw):
    return Library(name=name, experiment=exp, target_m=target, i7_seq=i7, i5_seq=i5,
                   r1=28, r2=90, i7_len=8, i5_len=8, **kw)


SPEC = RunSpec(r1=28, r2=90, i7_len=8, i5_len=8, flowcell_output_m=1000, safety_margin_percent=10)


def codes(issues, level=None):
    return {i.code for i in issues if level is None or i.level == level}


def test_hamming():
    assert hamming("ACGT", "ACGA") == 1


def test_clean_unique_dual_run_has_no_errors():
    libs = [lib("A", "ACGTACGT", "TGCATGCA"), lib("B", "CATGCATG", "GTACGTAC")]
    issues = check_run(libs, SPEC, CFG)
    assert not codes(issues, "error"), issues
    assert "index_hopping" not in codes(issues)


def test_duplicate_pair_is_error():
    issues = check_run([lib("A", "ACGTACGT", "TGCATGCA"), lib("B", "ACGTACGT", "TGCATGCA")], SPEC, CFG)
    assert "duplicate_index" in codes(issues, "error")


def test_close_indexes_error_but_one_distant_index_is_enough():
    close = [lib("A", "ACGTACGT", "TGCATGCA"), lib("B", "ACGTACGA", "TGCATGCT")]  # 1 and 1 mismatch
    assert "index_distance" in codes(check_run(close, SPEC, CFG), "error")
    shared_i7 = [lib("A", "ACGTACGT", "TGCATGCA"), lib("B", "ACGTACGT", "CATGCATG")]  # same i7, distant i5
    issues = check_run(shared_i7, SPEC, CFG)
    assert "index_distance" not in codes(issues)
    assert "index_hopping" in codes(issues, "warning")


def test_read_structure_mismatch():
    other = Library(name="X", experiment="E2", target_m=5, i7_seq="CATGCATG", i5_seq="GTACGTAC", r1=150, r2=150, i7_len=8, i5_len=8)
    issues = check_run([lib("A", "ACGTACGT", "TGCATGCA"), other], SPEC, CFG)
    assert "read_structure" in codes(issues, "error")


def test_all_g_cycle_is_error():
    issues = check_run([lib("A", "GGACGTAC", "TGCATGCA"), lib("B", "GGTGCATG", "CATGCATG")], SPEC, CFG)
    assert "colour_dark" in codes(issues, "error")


def test_capacity_over_flowcell():
    issues = check_run([lib("A", "ACGTACGT", "TGCATGCA", target=950)], SPEC, CFG)
    assert "capacity" in codes(issues, "error")


def test_low_complexity_uses_more_phix():
    small = RunSpec(r1=28, r2=90, i7_len=8, i5_len=8, flowcell_output_m=100, safety_margin_percent=0)
    normal = check_run([lib("A", "ACGTACGT", "TGCATGCA", target=95)], small, CFG)
    low = check_run([lib("A", "ACGTACGT", "TGCATGCA", target=95, low_complexity=True)], small, CFG)
    assert "capacity" not in codes(normal, "error")
    assert "capacity" in codes(low, "error")


def test_longer_index_is_truncated_not_error():
    issues = check_run([lib("A", "ACGTACGTAA", "TGCATGCA"), lib("B", "CATGCATGTT", "GTACGTAC")], SPEC, CFG)
    assert "truncated_index" in codes(issues, "info")
    assert not codes(issues, "error")


def test_flowcell_fit():
    rows = flowcell_fit(900, 1, 10, [("small", 1000), ("big", 10000)], typical_experiment_m=400)
    need = required_output(900, 1, 10)
    assert rows[0]["needed_m"] == need and rows[0]["fits"] is False
    assert rows[1]["fits"] and rows[1]["more_experiments"] >= 19
