"""Multiwell plate geometry and the STAR PCR-plate layout — pure Python, no database.

Well order is always column-wise (A1, B1, C1, …, A2, …), the order in which the STAR
protocol walks a plate.
"""

from __future__ import annotations

import re
import string
from dataclasses import dataclass

# format (number of wells) → (rows, columns)
FORMATS = {6: (2, 3), 12: (3, 4), 24: (4, 6), 48: (6, 8), 96: (8, 12)}
DEFAULT_FORMAT = 24
STAR_SUPPORTED_FORMATS = {24}  # tag_and_pool_hamilton_star_sim currently loads 24-well culture plates only


def rows_cols(fmt: int) -> tuple[int, int]:
    return FORMATS[fmt]


def wells(fmt: int) -> list[str]:
    """All wells of a plate format in column-wise order."""
    rows, cols = FORMATS[fmt]
    return [f"{string.ascii_uppercase[r]}{c}" for c in range(1, cols + 1) for r in range(rows)]


def well_index(fmt: int, well: str) -> int:
    try:
        return wells(fmt).index(well)
    except (ValueError, KeyError):
        return 10**6


def parse_format(value) -> int | None:
    """'24', '24-well', '24 well', 24.0 → 24. Empty → None. Unknown → raises ValueError."""
    if value in (None, ""):
        return None
    m = re.match(r"^\s*(\d+)", str(value))
    if not m or int(m.group(1)) not in FORMATS:
        raise ValueError(f"unknown plate format '{value}' (allowed: {', '.join(map(str, FORMATS))}-well)")
    return int(m.group(1))


WELLS_96 = wells(96)


@dataclass
class PcrPosition:
    plate: str  # PCR1, PCR2, …
    well: str


def pcr_layout(samples: list[dict]) -> dict[str, PcrPosition]:
    """Where each sample sits in the 96-well PCR (annealing) plates.

    Same rules as `plan_layout` in tag_and_pool_hamilton_star_sim/src/libprep/samplesheet.py:
    * pools in order of first appearance in the sample list,
    * inside a pool: by culture plate (order of first appearance), then column-wise well order,
    * every pool starts in a new column; a pool never spans two PCR plates.

    `samples`: dicts with sample_id, pool_id, source_plate, source_well, plate_format (list order = sheet order).
    """
    plate_order = {p: i for i, p in enumerate(dict.fromkeys(s["source_plate"] for s in samples))}
    pools = list(dict.fromkeys(s["pool_id"] for s in samples if s["pool_id"]))
    out: dict[str, PcrPosition] = {}
    plate_idx, col = 0, 0
    for pool in pools:
        members = sorted((s for s in samples if s["pool_id"] == pool),
                         key=lambda s: (plate_order[s["source_plate"]],
                                        well_index(s.get("plate_format") or DEFAULT_FORMAT, s["source_well"])))
        n_cols = -(-len(members) // 8)
        if col + n_cols > 12:
            plate_idx, col = plate_idx + 1, 0
        for i, s in enumerate(members):
            out[s["sample_id"]] = PcrPosition(f"PCR{plate_idx + 1}", WELLS_96[col * 8 + i])
        col += n_cols
    return out


def grid(fmt: int, filled: dict[str, object]) -> list[list[tuple[str, object]]]:
    """Rows of (well, content-or-None) for rendering a plate as a table."""
    rows, cols = FORMATS[fmt]
    return [[(f"{string.ascii_uppercase[r]}{c}", filled.get(f"{string.ascii_uppercase[r]}{c}"))
             for c in range(1, cols + 1)] for r in range(rows)]


# Up to 24 well-separated colours for pools / experiments (light fill + dark text).
def colour(i: int) -> str:
    hue = (i * 137.508) % 360  # golden-angle spacing
    return f"hsl({hue:.0f} 70% 84%)"


def colour_dark(i: int) -> str:
    hue = (i * 137.508) % 360
    return f"hsl({hue:.0f} 55% 32%)"
