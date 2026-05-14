# Task 10.5: Chromosome shard planner for submit-run.py.
"""FAI-driven shard planner.

Requirements: 17.8
Design: D7 (chromosome sharding for read-based callers), Property 18.

Property 18 states: *for any reference FAI file listing contigs
C = {c_1, c_2, …, c_n} and any boolean shard_by_chromosome, the shard plan
SHALL satisfy: when shard_by_chromosome is true, the plan contains exactly n
shards, one per contig, and the union of the shard regions equals the full
reference; when shard_by_chromosome is false, the plan contains exactly one
shard covering the full reference.*

Each :class:`Region` is a half-open ``[start, end)`` interval expressed in
0-based coordinates, matching BED semantics and ``sniffles2 --regions`` usage
(``chr:start-end`` strings are produced by :meth:`Region.to_regions_str`).
"""

from __future__ import annotations

from dataclasses import dataclass


# Sentinel contig name used when ``shard_by_chromosome=False`` collapses the
# reference into a single whole-genome region. Kept as a module constant so
# the property test and the WDL side can agree on the spelling.
WHOLE_REFERENCE_CONTIG = "WHOLE"


@dataclass(frozen=True)
class Region:
    """A half-open genomic interval ``[start, end)`` on a single contig."""

    contig: str
    start: int
    end: int

    def __post_init__(self) -> None:  # pragma: no cover - dataclass plumbing
        if not isinstance(self.contig, str) or not self.contig:
            raise ValueError(f"Region.contig must be a non-empty string (got {self.contig!r})")
        if not isinstance(self.start, int) or self.start < 0:
            raise ValueError(f"Region.start must be a non-negative int (got {self.start!r})")
        if not isinstance(self.end, int) or self.end <= self.start:
            raise ValueError(
                f"Region.end must be an int strictly greater than start "
                f"(got start={self.start!r}, end={self.end!r})"
            )

    @property
    def length(self) -> int:
        return self.end - self.start

    def to_regions_str(self) -> str:
        """Return the ``contig:start-end`` form accepted by Sniffles2/PBSV."""
        return f"{self.contig}:{self.start}-{self.end}"


def parse_fai(fai_contents: str) -> list[Region]:
    """Parse a .fai text blob into one :class:`Region` per contig.

    A .fai record is five tab-separated columns: ``name``, ``length``,
    ``offset``, ``linebases``, ``linewidth``. Only the first two are relevant
    for sharding; the others are ignored. Empty and comment (``#``) lines are
    skipped. Malformed lines (fewer than two fields, non-integer length, or a
    non-positive length) raise :class:`ValueError` with the offending line.
    """
    regions: list[Region] = []
    for lineno, raw in enumerate(fai_contents.splitlines(), start=1):
        line = raw.rstrip("\r")
        if not line.strip() or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            raise ValueError(
                f"parse_fai: line {lineno} has < 2 tab-separated fields: {raw!r}"
            )
        contig = parts[0]
        try:
            length = int(parts[1])
        except ValueError as exc:
            raise ValueError(
                f"parse_fai: line {lineno} has non-integer length: {raw!r}"
            ) from exc
        if length <= 0:
            raise ValueError(
                f"parse_fai: line {lineno} has non-positive contig length "
                f"{length} for contig {contig!r}"
            )
        regions.append(Region(contig=contig, start=0, end=length))
    return regions


def plan_shards(fai_contents: str, shard_by_chromosome: bool) -> list[Region]:
    """Return the per-shard :class:`Region` list for the given FAI.

    * ``shard_by_chromosome=True`` — one ``Region`` per contig, covering the
      full contig length. Property 18 "exactly n shards, union equals the full
      reference".
    * ``shard_by_chromosome=False`` — exactly one synthetic ``Region`` whose
      ``end`` equals the sum of contig lengths. Its contig name is the
      sentinel :data:`WHOLE_REFERENCE_CONTIG` so downstream code can recognise
      "no sharding" and feed ``--regions`` / ``-r`` accordingly (callers that
      do not support a "whole reference" pseudo-contig should gate on this
      sentinel and omit the region argument).

    Returns an empty list when the FAI has no contigs, which lets the caller
    fail fast with a clear error ("reference is empty") rather than submit a
    zero-shard run.
    """
    regions = parse_fai(fai_contents)
    if not regions:
        return []
    if shard_by_chromosome:
        return regions
    total_length = sum(r.length for r in regions)
    return [Region(contig=WHOLE_REFERENCE_CONTIG, start=0, end=total_length)]
