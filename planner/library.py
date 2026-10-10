"""Library structure: an ordered list of labelled segments from P5 to P7, and what each read covers.

A layout is a list of dicts: {"type": "insert", "label": "cDNA", "length": 90}
(length may be None for variable segments such as the insert).

Reads (Illumina, drawn on the top strand P5 → P7):
  R1       starts right after the Read 1 primer site, reads towards P7     (r1 cycles)
  Index 1  starts right after the Read 2 primer site, reads the i7 index   (i7 cycles)
  Index 2  ends right before the Read 1 primer site, covers the i5 index   (i5 cycles)
  R2       starts right before the Read 2 primer site, reads towards P5    (r2 cycles)
"""

from __future__ import annotations

from dataclasses import dataclass, field

SEGMENT_TYPES = {
    # type: (default label, default length, fill colour)
    "p5": ("P5", 29, "#c9d3e3"),
    "i5": ("i5 index", 8, "#f6c177"),
    "read1_primer": ("Read 1 primer site", 33, "#a7c7e7"),
    "insert": ("Insert (cDNA)", None, "#d8e8c8"),
    "well_bc": ("Well barcode", 8, "#f4a6a6"),
    "umi": ("UMI", 10, "#d7b8f3"),
    "polyt": ("poly(T)", 30, "#e9e3c6"),
    "linker": ("Linker", 6, "#e3e3e3"),
    "read2_primer": ("Read 2 primer site", 34, "#a7c7e7"),
    "i7": ("i7 index", 8, "#f6c177"),
    "p7": ("P7", 24, "#c9d3e3"),
    "custom": ("Custom", 10, "#ffffff"),
}

# Generic example — adjust to your construct in the builder and save it as template.
DEFAULT_LAYOUTS = {
    "tag_and_pool": ["p5", "i5", "read1_primer", "insert", "polyt", "umi", "well_bc", "read2_primer", "i7", "p7"],
    "_other": ["p5", "i5", "read1_primer", "insert", "read2_primer", "i7", "p7"],
}


def default_layout(library_type: str) -> list[dict]:
    types = DEFAULT_LAYOUTS.get(library_type, DEFAULT_LAYOUTS["_other"])
    return [{"type": t, "label": SEGMENT_TYPES[t][0], "length": SEGMENT_TYPES[t][1]} for t in types]


def clean_layout(raw) -> list[dict]:
    """Validate data coming from the browser."""
    if not isinstance(raw, list) or len(raw) > 40:
        raise ValueError("layout must be a list of at most 40 segments")
    out = []
    for seg in raw:
        t = str(seg.get("type", ""))
        if t not in SEGMENT_TYPES:
            raise ValueError(f"unknown segment type '{t}'")
        label = str(seg.get("label") or SEGMENT_TYPES[t][0]).strip()[:40]
        length = seg.get("length")
        if length in ("", None):
            length = None
        else:
            length = int(length)
            if not 0 < length <= 5000:
                raise ValueError(f"{label}: length must be 1–5000 bp")
        out.append({"type": t, "label": label, "length": length})
    return out


@dataclass
class Placed:
    index: int
    type: str
    label: str
    start: int  # 0-based bp position on the product
    length: int
    variable: bool


@dataclass
class Read:
    name: str
    cycles: int
    direction: int  # +1 towards P7, -1 towards P5
    start: int  # bp position of the first base read
    covers: list[dict] = field(default_factory=list)  # {"index", "type", "label", "first", "last"} (1-based read bases)
    end_of_product: bool = False


@dataclass
class Analysis:
    placed: list[Placed]
    total: int
    reads: list[Read]
    messages: list[tuple[str, str]]  # (level, text)


def analyse(layout: list[dict], r1: int | None, r2: int | None, i7: int | None, i5: int | None,
            insert_length: int | None, well_bc_setting: tuple[str, int, int] | None = None) -> Analysis:
    msgs: list[tuple[str, str]] = []
    placed, pos = [], 0
    for i, seg in enumerate(layout):
        variable = seg.get("length") is None
        length = seg.get("length") or (insert_length if seg["type"] == "insert" and insert_length else 100)
        placed.append(Placed(i, seg["type"], seg["label"], pos, length, variable))
        pos += length
    total = pos

    def find(t):
        return [p for p in placed if p.type == t]

    reads: list[Read] = []
    r1p, r2p = find("read1_primer"), find("read2_primer")
    if len(r1p) != 1:
        msgs.append(("error", "Exactly one 'Read 1 primer site' is needed."))
    if len(r2p) != 1:
        msgs.append(("error", "Exactly one 'Read 2 primer site' is needed."))
    if len(r1p) == 1 and len(r2p) == 1 and r1p[0].start > r2p[0].start:
        msgs.append(("error", "The Read 1 primer site must be on the P5 side of the Read 2 primer site."))
    if layout and layout[0]["type"] != "p5":
        msgs.append(("warning", "The product should start with P5."))
    if layout and layout[-1]["type"] != "p7":
        msgs.append(("warning", "The product should end with P7."))

    def make(name, cycles, start, direction):
        if not cycles:
            return
        rd = Read(name, cycles, direction, start)
        for b in range(cycles):
            x = start + direction * b
            if x < 0 or x >= total:
                rd.end_of_product = True
                break
            seg = next(p for p in placed if p.start <= x < p.start + p.length)
            if rd.covers and rd.covers[-1]["index"] == seg.index:
                rd.covers[-1]["last"] = b + 1
            else:
                rd.covers.append({"index": seg.index, "type": seg.type, "label": seg.label, "first": b + 1, "last": b + 1})
        reads.append(rd)

    if len(r1p) == 1 and len(r2p) == 1 and r1p[0].start < r2p[0].start:
        a, b = r1p[0], r2p[0]
        make("Read 1", r1, a.start + a.length, +1)
        make("Index 1 (i7)", i7, b.start + b.length, +1)
        make("Index 2 (i5)", i5, a.start - 1, -1)
        make("Read 2", r2, b.start - 1, -1)
        for rd in reads:
            if rd.end_of_product:
                msgs.append(("warning", f"{rd.name} runs past the end of the product."))
        for rd, want in (("Index 1 (i7)", "i7"), ("Index 2 (i5)", "i5")):
            r = next((x for x in reads if x.name == rd), None)
            if r and not any(c["type"] == want for c in r.covers):
                msgs.append(("warning", f"{rd} does not read an {want} segment."))
        # insert shorter than the reads → read-through into the opposite adapter
        ins = find("insert")
        if ins and r1 and ins[0].length < r1:
            msgs.append(("info", f"Insert (~{ins[0].length} bp) is shorter than Read 1 ({r1} cycles): read-through into the next segments."))

    # does the well barcode sit where the barcode map says?
    wb = find("well_bc")
    if wb and well_bc_setting:
        read_name, start, length = well_bc_setting
        rd = next((x for x in reads if x.name == {"R1": "Read 1", "R2": "Read 2"}.get(read_name)), None)
        hit = rd and any(c["type"] == "well_bc" and c["first"] == start and c["last"] - c["first"] + 1 == length
                         for c in rd.covers)
        if rd and hit:
            msgs.append(("ok", f"Well barcode = {read_name} bases {start}–{start + length - 1}, as in the barcode map."))
        elif rd:
            got = [f"{read_name} {c['first']}–{c['last']}" for c in rd.covers if c["type"] == "well_bc"]
            msgs.append(("error", f"Barcode map expects the well barcode at {read_name} {start}–{start + length - 1}, "
                                  f"this structure puts it at {', '.join(got) or 'no read position'}. "
                                  "Fix the structure or the WELL_BARCODE settings."))
    return Analysis(placed, total, reads, msgs)


def svg(an: Analysis, width: int = 1100) -> str:
    """Schematic: blocks proportional to length (with a minimum width), reads as arrows below."""
    if not an.placed:
        return ""
    min_w, pad = 46, 10
    raw = [max(p.length, 1) for p in an.placed]
    avail = width - 2 * pad
    # proportional widths, but every block at least min_w px (iterate: fix small blocks, rescale the rest)
    fixed: set[int] = set()
    while True:
        free = avail - min_w * len(fixed)
        rest = sum(r for i, r in enumerate(raw) if i not in fixed) or 1
        widths = [min_w if i in fixed else r * free / rest for i, r in enumerate(raw)]
        small = {i for i, w in enumerate(widths) if w < min_w and i not in fixed}
        if not small or len(fixed) + len(small) >= len(raw):
            widths = [max(w, min_w) for w in widths]
            break
        fixed |= small
    xs, x = [], pad
    for w in widths:
        xs.append(x)
        x += w

    def bp_to_x(bp: float) -> float:
        for p, x0, w in zip(an.placed, xs, widths):
            if p.start <= bp <= p.start + p.length:
                return x0 + (bp - p.start) / p.length * w
        return xs[-1] + widths[-1] if bp > 0 else pad

    esc = lambda s: str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    y0, h = 28, 44
    parts = [f'<svg viewBox="0 0 {width} {y0 + h + 30 + 30 * len(an.reads) + 10}" class="libsvg" role="img" '
             f'aria-label="Library structure schematic">',
             '<defs><marker id="ah" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="10" markerHeight="10" '
             'markerUnits="userSpaceOnUse" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="context-stroke"/></marker></defs>',
             f'<text x="{pad}" y="16" class="lbl">5′ P5 side</text>',
             f'<text x="{width - pad}" y="16" class="lbl" text-anchor="end">P7 side 3′</text>']
    for p, x0, w in zip(an.placed, xs, widths):
        fill = SEGMENT_TYPES[p.type][2]
        ln = "~" + str(p.length) if p.variable else str(p.length)
        parts.append(f'<g><title>{esc(p.label)} · {ln} bp</title>'
                     f'<rect x="{x0:.1f}" y="{y0}" width="{w:.1f}" height="{h}" rx="3" fill="{fill}" stroke="#55606e"/>'
                     f'<text x="{x0 + w / 2:.1f}" y="{y0 + 19}" text-anchor="middle" class="seg">{esc(p.label[:int(w / 6.5)] or "…")}</text>'
                     f'<text x="{x0 + w / 2:.1f}" y="{y0 + 35}" text-anchor="middle" class="len">{ln} bp</text></g>')
    y = y0 + h + 26
    for rd in an.reads:
        first = rd.start if rd.direction > 0 else rd.start + 1
        last = rd.start + rd.direction * rd.cycles
        last = min(max(last, 0), an.total)
        xa, xb = bp_to_x(first), bp_to_x(last)
        colour = "#2457c5" if rd.name.startswith("Read") else "#a15c00"
        parts.append(f'<line x1="{xa:.1f}" y1="{y}" x2="{xb:.1f}" y2="{y}" stroke="{colour}" stroke-width="3" marker-end="url(#ah)"/>')
        tx = min(xa, xb)
        anchor = "start"
        if tx > width * 0.6:
            tx, anchor = max(xa, xb), "end"
        parts.append(f'<text x="{tx:.1f}" y="{y - 7}" text-anchor="{anchor}" class="rd" fill="{colour}">'
                     f'{esc(rd.name)} · {rd.cycles} cycles</text>')
        y += 30
    parts.append("</svg>")
    return "".join(parts)
