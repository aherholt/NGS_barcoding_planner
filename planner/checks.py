"""Run-plan checks — pure Python, no database, so they are easy to test and reason about.

A sequencing run is described as a list of `Library` objects (one per indexed
pool). `check_run` returns a list of `Issue`s:

  error    the run must not be submitted (e.g. two libraries cannot be told apart)
  warning  allowed, but someone should look at it (e.g. weak colour balance)
  info     for information only

Background for the checks
-------------------------
* Read structure: all libraries on one flow cell are sequenced with the same
  number of cycles for R1, i7 (Index 1), i5 (Index 2) and R2.
* Index distance: the provider's demultiplexer tolerates sequencing errors
  (typically 1 mismatch per index). Two libraries are only safely separable if
  their i7 OR their i5 differ in at least MIN_INDEX_HAMMING_DISTANCE positions.
* Index hopping: on patterned flow cells (NovaSeq X) a small fraction of reads
  swap their index. With a combinatorial set (i7 or i5 shared between
  libraries) hopped reads can be assigned to the WRONG library. Unique dual
  indexes avoid that; with 12 i7 × 18 i5 that is possible for up to 12 libraries.
* Colour balance: 2-channel chemistry (NovaSeq X) needs signal in both channels
  in every index cycle. G gives no signal.
* Capacity: requested read pairs + PhiX + safety margin must fit the flow cell.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations
from typing import Iterable, Optional


@dataclass
class Library:
    name: str
    experiment: str
    target_m: float = 0.0
    i7_id: str = ""
    i7_seq: str = ""
    i5_id: str = ""
    i5_seq: str = ""
    r1: Optional[int] = None
    r2: Optional[int] = None
    i7_len: Optional[int] = None
    i5_len: Optional[int] = None
    low_complexity: bool = False


@dataclass
class Issue:
    level: str  # "error" | "warning" | "info"
    code: str
    message: str


@dataclass
class RunSpec:
    r1: Optional[int]
    r2: Optional[int]
    i7_len: Optional[int]
    i5_len: Optional[int]
    flowcell_output_m: Optional[float] = None
    phix_percent: Optional[float] = None  # None = automatic
    safety_margin_percent: float = 10.0


def hamming(a: str, b: str) -> int:
    if len(a) != len(b):
        raise ValueError("sequences must have equal length")
    return sum(x != y for x, y in zip(a, b))


def effective_phix(libs: Iterable[Library], spec: RunSpec, default: float, low_complexity: float) -> float:
    if spec.phix_percent is not None:
        return spec.phix_percent
    return low_complexity if any(lib.low_complexity for lib in libs) else default


def required_output(total_target_m: float, phix_percent: float, margin_percent: float) -> float:
    """Flow cell output needed so that `total_target_m` read pairs remain after PhiX and margin."""
    return total_target_m * (1 + margin_percent / 100) / (1 - phix_percent / 100)


def check_run(libs: list[Library], spec: RunSpec, cfg: dict) -> list[Issue]:
    issues: list[Issue] = []
    if not libs:
        return [Issue("info", "empty", "No libraries in this run yet.")]

    # 1. read structure -----------------------------------------------------
    run_lengths = {"R1": spec.r1, "R2": spec.r2, "i7": spec.i7_len, "i5": spec.i5_len}
    missing_run = [k for k, v in run_lengths.items() if v is None]
    if missing_run:
        issues.append(Issue("error", "run_lengths", f"Run read lengths not set: {', '.join(missing_run)}."))
    mismatched: dict[str, set] = {}
    for lib in libs:
        lib_lengths = {"R1": lib.r1, "R2": lib.r2, "i7": lib.i7_len, "i5": lib.i5_len}
        for k, v in lib_lengths.items():
            if v is None:
                mismatched.setdefault(lib.experiment, set()).add(f"{k} not specified")
            elif run_lengths[k] is not None and v != run_lengths[k]:
                mismatched.setdefault(lib.experiment, set()).add(f"{k} {v} ≠ run {run_lengths[k]}")
    for exp, problems in sorted(mismatched.items()):
        issues.append(Issue("error", "read_structure", f"Experiment {exp}: {'; '.join(sorted(problems))}."))

    # 2. indexes present and long enough -----------------------------------
    n7 = spec.i7_len or 0
    n5 = spec.i5_len or 0
    no_index = [lib.name for lib in libs if not lib.i7_seq or (n5 and not lib.i5_seq)]
    if no_index:
        issues.append(Issue("error", "missing_index", f"No index assigned: {', '.join(no_index)}."))
    indexed = [lib for lib in libs if lib.name not in no_index]
    short = [lib.name for lib in indexed if len(lib.i7_seq) < n7 or len(lib.i5_seq) < n5]
    if short:
        issues.append(Issue("error", "short_index", f"Index shorter than the index read: {', '.join(short)}."))
        indexed = [lib for lib in indexed if lib.name not in short]
    longer = [lib.name for lib in indexed if len(lib.i7_seq) > n7 or (n5 and len(lib.i5_seq) > n5)]
    if longer and n7:
        issues.append(Issue("info", "truncated_index",
                            f"Only the first {n7}/{n5} bases of the indexes are read for {len(longer)} libraries; checks use the truncated sequences."))

    def keys(lib):
        return lib.i7_seq[:n7], lib.i5_seq[:n5] if n5 else ""

    # 3. duplicates and distance -------------------------------------------
    min_d = cfg["MIN_INDEX_HAMMING_DISTANCE"]
    seen: dict[tuple, str] = {}
    for lib in indexed:
        k = keys(lib)
        if k in seen:
            issues.append(Issue("error", "duplicate_index", f"{lib.name} and {seen[k]} have the same index pair."))
        else:
            seen[k] = lib.name
    if n7:
        close = []
        for a, b in combinations(indexed, 2):
            ka, kb = keys(a), keys(b)
            if ka == kb:
                continue
            d7 = hamming(ka[0], kb[0])
            d5 = hamming(ka[1], kb[1]) if n5 else 0
            if max(d7, d5) < min_d:
                close.append(f"{a.name}/{b.name} (i7 {d7}, i5 {d5})")
        if close:
            issues.append(Issue("error", "index_distance",
                                f"Index pairs closer than {min_d} mismatches in both indexes: " + "; ".join(close[:10])
                                + (f" … and {len(close) - 10} more" if len(close) > 10 else "")))

    # 4. index hopping risk (combinatorial indexing) -------------------------
    shared_i7 = len(indexed) - len({keys(lib)[0] for lib in indexed})
    shared_i5 = len(indexed) - len({keys(lib)[1] for lib in indexed}) if n5 else 0
    if shared_i7 or shared_i5:
        issues.append(Issue("warning", "index_hopping",
                            f"Combinatorial indexing: {shared_i7} librar{'y' if shared_i7 == 1 else 'ies'} share(s) an i7 and "
                            f"{shared_i5} share(s) an i5 with another library. "
                            "Index hopping can misassign a small fraction of reads between them. Prefer unique i7 AND i5 per library where possible."))

    # 5. colour balance (2-channel) -----------------------------------------
    channel_map = cfg["TWO_CHANNEL_MAP"]
    min_frac = cfg["MIN_CHANNEL_FRACTION"]
    weights = [max(lib.target_m, 0.0) or 1.0 for lib in indexed]
    total_w = sum(weights)
    for label, n, pick in (("i7", n7, 0), ("i5", n5, 1)):
        if not indexed or not n:
            continue
        weak, dark = [], []
        for cycle in range(n):
            frac = {"ch1": 0.0, "ch2": 0.0}
            for lib, w in zip(indexed, weights):
                base = keys(lib)[pick][cycle]
                for ch in channel_map.get(base, set()):
                    frac[ch] += w / total_w
            if frac["ch1"] == 0 and frac["ch2"] == 0:
                dark.append(cycle + 1)
            elif min(frac.values()) < min_frac:
                weak.append(cycle + 1)
        if dark:
            issues.append(Issue("error", "colour_dark", f"{label}: no signal at all in cycle(s) {dark} (only G bases)."))
        if weak:
            issues.append(Issue("warning", "colour_balance",
                                f"{label}: one channel below {min_frac:.0%} of signal in cycle(s) {weak}. Consider other index combinations or more PhiX."))

    # 6. capacity -------------------------------------------------------------
    total = sum(lib.target_m for lib in libs)
    if any(lib.target_m <= 0 for lib in libs):
        issues.append(Issue("warning", "no_target", "Some libraries have no read target."))
    if spec.flowcell_output_m:
        phix = effective_phix(libs, spec, cfg["DEFAULT_PHIX_PERCENT"], cfg["LOW_COMPLEXITY_PHIX_PERCENT"])
        needed = required_output(total, phix, spec.safety_margin_percent)
        fill = needed / spec.flowcell_output_m
        level = "error" if fill > 1 else "info"
        issues.append(Issue(level, "capacity",
                            f"Needs {needed:,.0f} M read pairs ({total:,.0f} M requested + {phix:g}% PhiX + "
                            f"{spec.safety_margin_percent:g}% margin) = {fill:.0%} of the flow cell."))
    else:
        issues.append(Issue("warning", "no_flowcell", "No flow cell type selected — capacity not checked."))

    return issues


def flowcell_fit(total_target_m: float, phix_percent: float, margin_percent: float,
                 flowcells: list[tuple[str, float]], typical_experiment_m: Optional[float] = None) -> list[dict]:
    """For every flow cell option: how full it would be and how much room is left.

    `typical_experiment_m` (e.g. median requested reads of recent experiments) is used
    to express the free space as "about N more experiments".
    """
    needed = required_output(total_target_m, phix_percent, margin_percent)
    rows = []
    for name, output in flowcells:
        free_output = max(output - needed, 0.0)
        # convert free flow-cell output back into requestable read pairs
        free_requestable = free_output * (1 - phix_percent / 100) / (1 + margin_percent / 100)
        rows.append({
            "name": name,
            "output_m": output,
            "needed_m": needed,
            "fill": needed / output if output else 0,
            "fits": needed <= output,
            "free_requestable_m": free_requestable,
            "more_experiments": int(free_requestable // typical_experiment_m) if typical_experiment_m else None,
        })
    return rows
