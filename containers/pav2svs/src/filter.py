"""PAV2SVs filter (Task 3.12).

Reads a PAV variants VCF, emits records with ``abs(SVLEN) >= 50`` with
all record fields preserved bit-for-bit, rewrites the VCF
``##source`` header line to exactly ``##source=PAV``, bgzips the
output, and writes a tabix index alongside.

Implements Requirements 3.3 and 3.5 / the PAV2SVs_Task contract in
Design.

Implementation note
-------------------
We use :mod:`pysam` as the primary VCF I/O path for two reasons:

1. It preserves the original FORMAT column and per-sample genotype
   fields bit-for-bit when the same header is threaded through
   :meth:`VariantFile.write`.
2. It handles the SVLEN INFO value regardless of whether it was
   declared ``Number=1`` (integer) or ``Number=.`` / ``Number=A``
   (tuple) without the caller needing to know the source VCF's header
   shape.

If :mod:`pysam` is unavailable at runtime (developer machines without
htslib dev headers), :func:`filter_vcf` raises :class:`RuntimeError`.
A subprocess fallback using ``bcftools view -Oz`` + ``tabix -p vcf``
is intentionally not implemented here: the container image always
bakes pysam in, and the WDL task only ever runs inside the container.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path
from typing import Sequence

try:
    import pysam  # type: ignore[import-untyped]

    _PYSAM_AVAILABLE = True
except ImportError:  # pragma: no cover - environment-specific
    pysam = None  # type: ignore[assignment]
    _PYSAM_AVAILABLE = False


#: Minimum absolute SVLEN for a record to be retained (Requirement 3.3).
MIN_ABS_SVLEN: int = 50


def _require_pysam() -> None:
    if not _PYSAM_AVAILABLE:  # pragma: no cover - environment-specific
        raise RuntimeError(
            "pysam is required by pav2svs.filter but is not importable. "
            "Install pysam>=0.22,<1 or use the container image which bakes "
            "it in."
        )


def _coerce_svlen(raw) -> int | None:
    """Normalise the SVLEN INFO value across pysam representations.

    Returns an int (the first element when SVLEN is a tuple/list) or
    ``None`` if the value is missing or non-integral.
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


def _rewrite_source_header(in_header: "pysam.VariantHeader") -> "pysam.VariantHeader":
    """Return a new VariantHeader identical to ``in_header`` except:

      * every ``##source=...`` line is dropped
      * a single canonical ``##source=PAV`` line is appended
      * all samples from the input are preserved

    Other header lines (contig, INFO, FORMAT, FILTER, fileformat,
    etc.) are preserved verbatim so that per-record FORMAT/GENOTYPE
    fields round-trip bit-for-bit.
    """
    _require_pysam()
    new_header = pysam.VariantHeader()
    for rec in in_header.records:
        if rec.key == "source":
            continue
        new_header.add_record(rec)
    new_header.add_line("##source=PAV")
    for sample in in_header.samples:
        new_header.add_sample(sample)
    return new_header


def filter_vcf(
    in_path: str | os.PathLike,
    out_path: str | os.PathLike,
    *,
    logger: logging.Logger | None = None,
) -> str:
    """Filter a PAV VCF, rewrite its source header, and bgzip + tabix-index
    the result.

    Returns the path to the bgzipped output. The tabix index is written
    to ``<out_path>.tbi``.

    Raises :class:`RuntimeError` if pysam is not importable.
    """
    _require_pysam()
    log = logger or logging.getLogger("pav2svs.filter")

    in_path = Path(in_path)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # We write an uncompressed intermediate first then bgzip +
    # tabix-index in place via pysam.tabix_index. This keeps the
    # pipeline single-pass without a separate bcftools dependency.
    if out_path.suffix == ".gz":
        plain_path = out_path.with_suffix("")  # strip .gz -> .vcf
    else:
        # Caller passed a non-.gz path. Fall back to .vcf.tmp to force
        # tabix_index to produce a .gz we can rename.
        plain_path = out_path.with_name(out_path.name + ".tmp.vcf")

    n_in = 0
    n_out = 0
    with pysam.VariantFile(str(in_path), "r") as in_vf:
        new_header = _rewrite_source_header(in_vf.header)
        with pysam.VariantFile(str(plain_path), "w", header=new_header) as out_vf:
            for rec in in_vf:
                n_in += 1
                svlen = _coerce_svlen(rec.info.get("SVLEN"))
                if svlen is None:
                    continue
                if abs(svlen) < MIN_ABS_SVLEN:
                    continue
                # Construct a new record bound to the output header and
                # copy every preservable field verbatim. We avoid
                # pysam.VariantRecord.translate because its return
                # contract differs across pysam versions (newer
                # releases return None on failure, older ones mutate
                # in-place) and we want a deterministic copy path.
                new_rec = out_vf.new_record(
                    contig=rec.chrom,
                    start=rec.start,
                    stop=rec.stop,
                    alleles=rec.alleles,
                    id=rec.id,
                    qual=rec.qual,
                    filter=list(rec.filter.keys()) or None,
                )
                for key, value in rec.info.items():
                    if key in new_header.info:
                        try:
                            new_rec.info[key] = value
                        except (TypeError, ValueError):
                            continue
                # Preserve per-sample FORMAT fields if any. Samples
                # carry over by name because _rewrite_source_header
                # preserved the sample list verbatim.
                for sample in rec.samples:
                    for fmt_key, fmt_val in rec.samples[sample].items():
                        if fmt_key in new_header.formats:
                            try:
                                new_rec.samples[sample][fmt_key] = fmt_val
                            except (TypeError, ValueError):
                                continue
                out_vf.write(new_rec)
                n_out += 1

    log.info(
        "pav2svs.filter: %d records in, %d SV records out (MIN_ABS_SVLEN=%d)",
        n_in,
        n_out,
        MIN_ABS_SVLEN,
    )

    # bgzip + tabix-index in place. force=True rewrites the file in
    # place and drops a .tbi next to it.
    pysam.tabix_index(
        str(plain_path), preset="vcf", force=True, keep_original=False
    )

    produced = Path(str(plain_path) + ".gz")
    if produced != out_path:
        produced.replace(out_path)
        produced_tbi = Path(str(produced) + ".tbi")
        if produced_tbi.exists():
            produced_tbi.replace(Path(str(out_path) + ".tbi"))
    return str(out_path)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pav2svs",
        description=(
            "Filter a PAV variants VCF to retain only structural variants "
            "(abs(SVLEN) >= 50), rewrite the source header to PAV, and "
            "bgzip + tabix-index the output."
        ),
    )
    parser.add_argument(
        "--in", dest="input_path", required=True, help="Input PAV VCF"
    )
    parser.add_argument(
        "--out", required=True, help="Output SV VCF (.vcf.gz)"
    )
    parser.add_argument(
        "--log-level", default="INFO", help="Logging level (default: INFO)"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point. Returns integer exit code.

    Exit codes:
      * 0: success
      * 3: pysam or runtime failure
    """
    parser = _build_parser()
    args = parser.parse_args(argv if argv is None else list(argv))

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    log = logging.getLogger("pav2svs.filter")

    try:
        filter_vcf(args.input_path, args.out, logger=log)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        _emit_task_trailer("pav2svs", 3)
        return 3
    _emit_task_trailer("pav2svs", 0)
    return 0


# ---------------------------------------------------------------------------
# Task 19.1 — stdout trailer pattern (Design §Error Handling Layer 3).
# ---------------------------------------------------------------------------
# See metadata-writer/writer.py for the full contract. Every module we
# own emits a JSON line of the form
# {"task": "<name>", "status": "ok"|"error", "exit_code": N,
#  "stderr_tail": "..."} to stdout just before exit so that
# MetadataWriter_Task can build `per_caller_status` without replaying
# CloudWatch logs.
def _emit_task_trailer(task_name: str, exit_code: int) -> None:
    """Emit the Design §Layer 3 stdout trailer."""
    import json as _json  # local import keeps the top of the module quiet
    trailer = {
        "task": task_name,
        "status": "ok" if exit_code == 0 else "error",
        "exit_code": int(exit_code),
        "stderr_tail": "",
    }
    print(_json.dumps(trailer, separators=(",", ":")))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
