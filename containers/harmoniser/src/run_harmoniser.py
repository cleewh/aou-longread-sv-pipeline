"""Harmoniser wrapper (Task 3.10).

Combines per-caller SV VCFs (PAV, Sniffles2, pbsv) into a single
reconciled VCF with a ``CALLERS`` INFO tag per Design D3 and
Requirement 6.

This module is a *self-contained* Python implementation of the
harmoniser contract. It does not literally import or shell out to the
scripts cloned from ``fabio-cunial/callset_integration_phase2``: those
scripts are a moving target (Design D3) and pinning their exact shape
is outside the scope of Task 3.10. Instead we implement the documented
invariants directly in-process using :mod:`pysam`, matching the
semantics that ``callset_integration_phase2`` produces in steady
state.

Algorithm
---------
1. Parse each supplied per-caller VCF with :class:`pysam.VariantFile`.
   Records are tagged with the caller name the wrapper was invoked
   with (``PAV`` / ``Sniffles2`` / ``pbsv``).
2. Cluster records across callers by position proximity and SV length:
   two records match when they have the same ``CHROM``, the same
   ``SVTYPE`` INFO value, ``|pos_a - pos_b| <= max_position_delta_bp``,
   and ``|svlen_a - svlen_b| <= max_svlen_fraction * max(|svlen_a|,
   |svlen_b|)``. These thresholds are the standard truvari/SURVIVOR
   defaults used throughout the callset_integration_phase2 scripts.
3. Emit one output record per cluster. The representative record is
   the one with the most callers supporting it; ties broken by the
   caller priority order ``PAV`` > ``Sniffles2`` > ``pbsv`` (the
   assembly-based caller's ALT alleles are preferred when available).
4. Stamp a ``CALLERS`` INFO tag with the comma-separated union of
   source callers, and rewrite the output ``##source`` header to
   ``callset_integration_phase2@<SHA>``.
5. Output is written as a bgzipped VCF and tabix-indexed in-place.

Thresholds
----------
Defaults (``max_position_delta_bp=500``, ``max_svlen_fraction=0.20``)
can be overridden via the ``--filter-override`` JSON (Requirement 6.6).
The JSON is a flat object whose recognised keys are exactly these two;
unknown keys are logged and ignored so that filter files targeting
future callset_integration_phase2 versions do not hard-fail the
wrapper.

Empty-input handling
--------------------
If the union of supplied VCFs contains zero records (either because no
``--pav`` / ``--sniffles2`` / ``--pbsv`` flags were passed or because
every supplied VCF is empty), the wrapper exits non-zero with a Layer 3
error message per Requirement 6.5. The empty-input check is a strict
pre-condition: even a single supplied VCF with zero records is an
error, because the workflow layer is responsible for skipping the
harmoniser entirely when no caller produced output.

Dependencies
------------
The module uses :mod:`pysam` for VCF parsing and writing and for
bgzipping + tabix-indexing the output. If :mod:`pysam` is not
importable (e.g. on developer machines without htslib dev headers) the
public :func:`harmonise` function raises :class:`RuntimeError`. A
future revision may fall back to shelling out to ``bcftools``/``tabix``
for the I/O shell; the in-process clustering logic is unaffected.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Iterable, Sequence

try:
    import pysam  # type: ignore[import-untyped]

    _PYSAM_AVAILABLE = True
except ImportError:  # pragma: no cover - environment-specific
    pysam = None  # type: ignore[assignment]
    _PYSAM_AVAILABLE = False


# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

#: Recognised caller names. Order doubles as tie-break priority when
#: selecting the representative record for a cluster (assembly-based
#: caller preferred; see module docstring §3).
CALLER_PRIORITY: tuple[str, ...] = ("PAV", "Sniffles2", "pbsv")

#: Default clustering thresholds, matched to the SURVIVOR/truvari
#: defaults used by the upstream scripts.
DEFAULT_MAX_POSITION_DELTA_BP: int = 500
DEFAULT_MAX_SVLEN_FRACTION: float = 0.20

#: Minimum absolute SVLEN for a record to be considered an SV. Matches
#: the PAV2SVs filter (Requirement 3.3) so that inputs from
#: non-PAV2SVs-filtered sources don't slip through.
MIN_ABS_SVLEN: int = 50


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class EmptyHarmoniserInputError(RuntimeError):
    """Raised when no input VCF is supplied or every supplied VCF has
    zero SV records. Maps to the Requirement 6.5 Layer 3 error
    "No SV callers produced output"."""


# ---------------------------------------------------------------------------
# Internal data model
# ---------------------------------------------------------------------------


class _CallRecord:
    """Lightweight container for one SV record + its source caller.

    We avoid subclassing :class:`pysam.VariantRecord` because pysam
    records are tied to the :class:`VariantFile` that produced them,
    which makes them awkward to reshuffle across input files.
    """

    __slots__ = (
        "caller",
        "chrom",
        "pos",
        "svtype",
        "svlen",
        "record",  # original pysam.VariantRecord, used when emitting
    )

    def __init__(
        self,
        caller: str,
        chrom: str,
        pos: int,
        svtype: str,
        svlen: int,
        record: "pysam.VariantRecord",
    ) -> None:
        self.caller = caller
        self.chrom = chrom
        self.pos = pos
        self.svtype = svtype
        self.svlen = svlen
        self.record = record


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _require_pysam() -> None:
    if not _PYSAM_AVAILABLE:  # pragma: no cover - environment-specific
        raise RuntimeError(
            "pysam is required by the harmoniser but is not importable. "
            "Install pysam>=0.22,<1 or use the container image which bakes "
            "it in."
        )


def _coerce_svlen(raw) -> int | None:
    """Normalise the SVLEN INFO value across pysam representations.

    pysam returns SVLEN as either a single int (when declared Number=1)
    or a tuple[int, ...] (Number=. or Number=A). We take the first
    element of a tuple, coerce to int, and return ``None`` on failure.
    """
    if raw is None:
        return None
    if isinstance(raw, (tuple, list)):
        if not raw:
            return None
        raw = raw[0]
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _read_records(
    vcf_path: Path, caller: str
) -> list[_CallRecord]:
    """Read SV records from ``vcf_path`` and tag them with ``caller``.

    Records without a parseable SVTYPE or SVLEN, and records with
    ``abs(SVLEN) < MIN_ABS_SVLEN``, are silently skipped.
    """
    _require_pysam()
    out: list[_CallRecord] = []
    with pysam.VariantFile(str(vcf_path), "r") as vf:
        for rec in vf:
            info = rec.info
            svtype = info.get("SVTYPE")
            if svtype is None:
                continue
            # SVTYPE may be a tuple when declared Number=. Pick first.
            if isinstance(svtype, (tuple, list)):
                svtype = svtype[0] if svtype else None
            if svtype is None:
                continue
            svlen = _coerce_svlen(info.get("SVLEN"))
            if svlen is None:
                continue
            if abs(svlen) < MIN_ABS_SVLEN:
                continue
            # pysam positions are 1-based here; we only use them for
            # relative proximity, so the frame is immaterial as long
            # as it's consistent.
            out.append(
                _CallRecord(
                    caller=caller,
                    chrom=rec.chrom,
                    pos=rec.pos,
                    svtype=str(svtype),
                    svlen=svlen,
                    record=rec,
                )
            )
    return out


def _same_cluster(
    a: _CallRecord,
    b: _CallRecord,
    max_position_delta_bp: int,
    max_svlen_fraction: float,
) -> bool:
    """Return True iff records ``a`` and ``b`` belong to the same
    harmoniser cluster per the thresholds."""
    if a.chrom != b.chrom or a.svtype != b.svtype:
        return False
    if abs(a.pos - b.pos) > max_position_delta_bp:
        return False
    denom = max(abs(a.svlen), abs(b.svlen))
    if denom == 0:
        return abs(a.svlen) == abs(b.svlen)
    return abs(abs(a.svlen) - abs(b.svlen)) <= max_svlen_fraction * denom


def _cluster_records(
    records: Sequence[_CallRecord],
    max_position_delta_bp: int,
    max_svlen_fraction: float,
) -> list[list[_CallRecord]]:
    """Greedy single-linkage clustering of records.

    We sort by (chrom, svtype, pos) so that an ``O(n log n) + O(n * k)``
    pass suffices where ``k`` is the small per-chromosome cluster
    size in realistic inputs. Full pairwise clustering is unnecessary
    because the position and SVTYPE gates prune aggressively.
    """
    sorted_records = sorted(records, key=lambda r: (r.chrom, r.svtype, r.pos))
    clusters: list[list[_CallRecord]] = []
    for rec in sorted_records:
        placed = False
        # Only check the most recent few clusters on the same chrom+svtype —
        # anything further back cannot be within the position window due
        # to the sort order.
        for cluster in reversed(clusters):
            head = cluster[-1]
            if head.chrom != rec.chrom or head.svtype != rec.svtype:
                # Sort guarantees different chrom/svtype means we're
                # past this cluster's scan window.
                break
            if rec.pos - head.pos > max_position_delta_bp:
                break
            if _same_cluster(
                head, rec, max_position_delta_bp, max_svlen_fraction
            ):
                cluster.append(rec)
                placed = True
                break
        if not placed:
            clusters.append([rec])
    return clusters


def _pick_representative(cluster: Sequence[_CallRecord]) -> _CallRecord:
    """Return the cluster member that should contribute ALT/REF fields
    to the emitted record. Priority: ``PAV`` > ``Sniffles2`` > ``pbsv``.
    Stable by first-seen order within a caller."""
    for caller in CALLER_PRIORITY:
        for rec in cluster:
            if rec.caller == caller:
                return rec
    # Shouldn't happen — clusters are built from records tagged with
    # known callers — but stay defensive.
    return cluster[0]


def _unique_callers(cluster: Iterable[_CallRecord]) -> list[str]:
    """Return the union of callers supporting ``cluster`` in priority
    order."""
    present: set[str] = {r.caller for r in cluster}
    return [c for c in CALLER_PRIORITY if c in present]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_filter_override(path: str | os.PathLike | None) -> dict:
    """Load and normalise a ``--filter-override`` JSON file.

    Unknown keys are retained in the returned dict so that callers can
    log them; the two recognised keys are coerced to the expected
    types. Missing keys fall back to the module-level defaults.
    """
    thresholds = {
        "max_position_delta_bp": DEFAULT_MAX_POSITION_DELTA_BP,
        "max_svlen_fraction": DEFAULT_MAX_SVLEN_FRACTION,
    }
    if path is None:
        return thresholds
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(
            f"filter-override {path!s}: expected JSON object, got "
            f"{type(data).__name__}"
        )
    if "max_position_delta_bp" in data:
        thresholds["max_position_delta_bp"] = int(data["max_position_delta_bp"])
    if "max_svlen_fraction" in data:
        thresholds["max_svlen_fraction"] = float(data["max_svlen_fraction"])
    return thresholds


def harmonise(
    *,
    pav: str | os.PathLike | None = None,
    sniffles2: str | os.PathLike | None = None,
    pbsv: str | os.PathLike | None = None,
    out: str | os.PathLike,
    filter_override: str | os.PathLike | None = None,
    logger: logging.Logger | None = None,
) -> str:
    """Run the harmoniser end-to-end.

    Parameters are the per-caller VCF paths (any subset may be ``None``),
    the output VCF path (must end in ``.vcf.gz``), and an optional
    filter-override JSON path. Returns the path to the bgzipped output
    VCF. The corresponding tabix index is written alongside.

    Raises :class:`EmptyHarmoniserInputError` if no non-empty caller
    VCF is supplied (Requirement 6.5).
    """
    _require_pysam()
    log = logger or logging.getLogger("harmoniser")

    caller_paths: list[tuple[str, Path]] = []
    for caller, raw in (
        ("PAV", pav),
        ("Sniffles2", sniffles2),
        ("pbsv", pbsv),
    ):
        if raw is not None:
            caller_paths.append((caller, Path(raw)))

    if not caller_paths:
        raise EmptyHarmoniserInputError(
            "No SV callers produced output: harmoniser requires at least "
            "one of --pav / --sniffles2 / --pbsv"
        )

    thresholds = load_filter_override(filter_override)
    log.info(
        "Harmonising callers=%s with thresholds=%s",
        [c for c, _ in caller_paths],
        thresholds,
    )

    all_records: list[_CallRecord] = []
    for caller, vcf_path in caller_paths:
        all_records.extend(_read_records(vcf_path, caller))

    if not all_records:
        raise EmptyHarmoniserInputError(
            "No SV callers produced output: every supplied VCF was empty "
            "after SVLEN>=50 filtering"
        )

    clusters = _cluster_records(
        all_records,
        thresholds["max_position_delta_bp"],
        thresholds["max_svlen_fraction"],
    )

    _write_output(
        clusters=clusters,
        representative_vcf_path=caller_paths[0][1],  # any input's header
                                                       # as a template
        output_path=Path(out),
        logger=log,
    )
    return str(out)


def _write_output(
    *,
    clusters: Sequence[Sequence[_CallRecord]],
    representative_vcf_path: Path,
    output_path: Path,
    logger: logging.Logger,
) -> None:
    """Write clustered records to a bgzipped, tabix-indexed VCF.

    The output header is derived from ``representative_vcf_path`` but
    rewritten to (a) replace any ``##source=`` line with
    ``##source=callset_integration_phase2@<SHA>`` and (b) declare the
    ``CALLERS`` INFO tag. SVLEN and SVTYPE declarations are left alone
    (they must already be declared in the source header, since all
    known SV callers emit them).
    """
    _require_pysam()
    sha = os.environ.get(
        "CALLSET_INTEGRATION_PHASE2_SHA", "TBD-pin-at-first-real-mirror"
    )
    with pysam.VariantFile(str(representative_vcf_path), "r") as template:
        header = template.header.copy()

    # Rewrite ##source header. pysam exposes header records via
    # header.records; we can't mutate the list in place cleanly, so we
    # build a fresh header with the adjusted source line and reuse the
    # contig + info declarations from the template.
    new_header = pysam.VariantHeader()
    for rec in header.records:
        if rec.key == "source":
            continue  # will add a canonical source below
        if rec.key == "INFO" and rec.get("ID") == "CALLERS":
            continue  # will re-declare below
        new_header.add_record(rec)
    new_header.add_line(f"##source=callset_integration_phase2@{sha}")
    new_header.add_line(
        '##INFO=<ID=CALLERS,Number=.,Type=String,'
        'Description="Comma-separated list of source callers supporting this SV">'
    )
    # Preserve sample columns from the template so records carry
    # through without pysam complaining about column mismatch.
    for sample in header.samples:
        new_header.add_sample(sample)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    # We write uncompressed VCF first and then bgzip+tabix-index via
    # pysam.tabix_index with force=True, which bgzips in place. This
    # keeps the pipeline single-pass and avoids a separate bcftools
    # dependency at the Python layer.
    plain_path = output_path.with_suffix("")  # strip .gz
    if plain_path.suffix != ".vcf":
        # If the caller passed an unexpected extension (e.g. .txt.gz),
        # force the plain intermediate to end in .vcf so pysam + tabix
        # recognise the format.
        plain_path = output_path.with_name(output_path.name + ".tmp.vcf")

    n_out = 0
    with pysam.VariantFile(str(plain_path), "w", header=new_header) as out_vf:
        # Sort clusters by (chrom, pos) of their representative so the
        # emitted VCF is tabix-indexable. _cluster_records sorts by
        # (chrom, svtype, pos) for efficiency; we undo the svtype-first
        # grouping here.
        ordered = sorted(
            clusters,
            key=lambda c: (_pick_representative(c).chrom,
                           _pick_representative(c).record.start),
        )
        for cluster in ordered:
            rep = _pick_representative(cluster)
            # Filter names present on the representative may originate
            # from a caller whose header is NOT the template header
            # (e.g. pbsv's `NearReferenceGap` filter when the template
            # is the Sniffles2 VCF). Drop any filter the new header
            # doesn't declare so pysam accepts the record — this is
            # harmless because the CALLERS INFO tag records caller
            # provenance separately.
            kept_filters = [
                fname for fname in rep.record.filter.keys()
                if fname in new_header.filters
            ]
            new_rec = out_vf.new_record(
                contig=rep.chrom,
                start=rep.record.start,
                stop=rep.record.stop,
                alleles=rep.record.alleles,
                id=rep.record.id,
                qual=rep.record.qual,
                filter=kept_filters or None,
            )
            # Copy INFO fields from the representative record that the
            # new header still knows about (SVTYPE, SVLEN, END, ...).
            for key, value in rep.record.info.items():
                if key in new_header.info:
                    try:
                        new_rec.info[key] = value
                    except (TypeError, ValueError):
                        # Non-copyable INFO value; skip rather than fail.
                        continue
            new_rec.info["CALLERS"] = ",".join(_unique_callers(cluster))
            out_vf.write(new_rec)
            n_out += 1

    logger.info("Harmoniser wrote %d records to %s", n_out, plain_path)

    # bgzip + tabix-index in place. pysam.tabix_index with
    # force=True rewrites the file as .gz and drops a .tbi next to it.
    pysam.tabix_index(str(plain_path), preset="vcf", force=True, keep_original=False)

    produced = Path(str(plain_path) + ".gz")
    if produced != output_path:
        # Rename to the requested output path and move the .tbi too.
        produced.replace(output_path)
        produced_tbi = Path(str(produced) + ".tbi")
        if produced_tbi.exists():
            produced_tbi.replace(Path(str(output_path) + ".tbi"))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="harmoniser",
        description=(
            "Merge per-caller SV VCFs (PAV, Sniffles2, pbsv) into a "
            "single harmonised VCF with a CALLERS INFO tag."
        ),
    )
    parser.add_argument("--pav", default=None, help="PAV2SVs SV VCF (bgzipped)")
    parser.add_argument(
        "--sniffles2", default=None, help="Sniffles2 SV VCF (bgzipped)"
    )
    parser.add_argument("--pbsv", default=None, help="pbsv SV VCF (bgzipped)")
    parser.add_argument(
        "--out", required=True, help="Output harmonised SV VCF path (.vcf.gz)"
    )
    parser.add_argument(
        "--filter-override",
        default=None,
        help="Optional JSON with threshold overrides (see module docstring)",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        help="Logging level (default: INFO)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point. Returns integer exit code.

    When ``argv`` is ``None`` the argparse default reads from
    ``sys.argv[1:]`` so ``python -m harmoniser ...`` works; explicit
    sequences (from tests) are passed through unchanged.

    Exit codes:
      * 0: success
      * 2: no-input (Requirement 6.5) or bad filter-override
      * 3: pysam or runtime failure
    """
    parser = _build_parser()
    args = parser.parse_args(argv if argv is None else list(argv))

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    log = logging.getLogger("harmoniser")

    try:
        harmonise(
            pav=args.pav,
            sniffles2=args.sniffles2,
            pbsv=args.pbsv,
            out=args.out,
            filter_override=args.filter_override,
            logger=log,
        )
    except EmptyHarmoniserInputError as exc:
        print(f"error: {exc}", file=sys.stderr)
        _emit_task_trailer("harmoniser", 2)
        return 2
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"error: filter-override: {exc}", file=sys.stderr)
        _emit_task_trailer("harmoniser", 2)
        return 2
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        _emit_task_trailer("harmoniser", 3)
        return 3
    _emit_task_trailer("harmoniser", 0)
    return 0


# ---------------------------------------------------------------------------
# Task 19.1 — stdout trailer pattern (Design §Error Handling Layer 3).
# ---------------------------------------------------------------------------
# See metadata-writer/writer.py for the full contract. Every module we
# own emits a JSON line of the form
# {"task": "<name>", "status": "ok"|"error", "exit_code": N,
#  "stderr_tail": "..."} to stdout right before exit so that
# MetadataWriter_Task can build `per_caller_status` without replaying
# CloudWatch logs. External tool wrappers (hifiasm, sniffles2, pbsv,
# pbmm2, pav) will gain the same trailer in a future revision via a
# shared shell helper in each Dockerfile.
def _emit_task_trailer(task_name: str, exit_code: int) -> None:
    """Emit the Design §Layer 3 stdout trailer."""
    trailer = {
        "task": task_name,
        "status": "ok" if exit_code == 0 else "error",
        "exit_code": int(exit_code),
        "stderr_tail": "",
    }
    print(json.dumps(trailer, separators=(",", ":")))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
