#!/usr/bin/env python3
# Task 13.3: render the HealthOmics execution-role policy from the Design template.
"""Render ``iam/execution_role_policy.json.tmpl`` to a concrete JSON document.

Requirements: 9.1, 9.3
Design: §IAM policy shape, Property 8.

The template contains five literal substitutions:

* ``${INPUT_BUCKET}``            — input S3 bucket name
* ``${OUTPUT_BUCKET}``           — output S3 bucket name
* ``${OUTPUT_PREFIX}``           — output prefix (trailing slash expected)
* ``${ACCOUNT_ID}``              — 12-digit AWS account id (logs ARN)
* ``${ECR_REPO_ARNS_JSON_ARRAY}`` — JSON array of ECR repo ARNs; rendered as
  a literal JSON list so the resulting document parses cleanly.

The rendered document is parsed once by :mod:`json` to catch malformed
substitutions immediately (Property 8's "parses as valid JSON" clause).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Core render API
# ---------------------------------------------------------------------------


def render(
    template_path: str | Path,
    input_bucket: str,
    output_bucket: str,
    output_prefix: str,
    ecr_repo_arns: list[str],
    account_id: str,
) -> dict:
    """Render the execution-role policy template and return the parsed dict.

    Raises:
        FileNotFoundError — ``template_path`` does not exist.
        ValueError        — rendered document fails to parse as JSON.
    """
    tmpl = Path(template_path).read_text(encoding="utf-8")
    rendered = _substitute(
        tmpl,
        input_bucket=input_bucket,
        output_bucket=output_bucket,
        output_prefix=output_prefix,
        ecr_repo_arns=list(ecr_repo_arns),
        account_id=account_id,
    )
    try:
        return json.loads(rendered)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Rendered IAM policy did not parse as JSON: {exc}\n---\n{rendered}"
        ) from exc


def render_to_file(
    template_path: str | Path,
    out_path: str | Path,
    input_bucket: str,
    output_bucket: str,
    output_prefix: str,
    ecr_repo_arns: list[str],
    account_id: str,
) -> Path:
    """Render the template and write the result to ``out_path`` (pretty-printed).

    Returns the ``Path`` written for caller convenience.
    """
    doc = render(
        template_path,
        input_bucket,
        output_bucket,
        output_prefix,
        ecr_repo_arns,
        account_id,
    )
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return out


# ---------------------------------------------------------------------------
# Substitution mechanics
# ---------------------------------------------------------------------------


def _substitute(
    tmpl: str,
    *,
    input_bucket: str,
    output_bucket: str,
    output_prefix: str,
    ecr_repo_arns: list[str],
    account_id: str,
) -> str:
    """Replace the five known placeholders in one pass.

    We deliberately avoid :class:`string.Template` because the template is
    handwritten JSONC-like text and we need to inject a JSON *array*
    literal for ``ECR_REPO_ARNS_JSON_ARRAY`` (not a quoted string) to
    produce a valid JSON document after substitution.
    """
    # The ECR ARN array is substituted first so that any literal ``$``
    # appearing in an ARN (there shouldn't be any, but be safe) is handled
    # by the remaining replacements rather than breaking the array.
    out = tmpl
    out = out.replace("${ECR_REPO_ARNS_JSON_ARRAY}", json.dumps(list(ecr_repo_arns)))
    out = out.replace("${INPUT_BUCKET}", input_bucket)
    out = out.replace("${OUTPUT_BUCKET}", output_bucket)
    out = out.replace("${OUTPUT_PREFIX}", output_prefix)
    out = out.replace("${ACCOUNT_ID}", account_id)
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="iam/render.py",
        description=(
            "Render iam/execution_role_policy.json.tmpl with the supplied "
            "substitution values and print the resulting JSON on stdout."
        ),
    )
    p.add_argument(
        "--template",
        required=True,
        type=Path,
        help="Path to iam/execution_role_policy.json.tmpl.",
    )
    p.add_argument(
        "--input-bucket",
        required=True,
        help="Input S3 bucket name (substitutes ${INPUT_BUCKET}).",
    )
    p.add_argument(
        "--output-bucket",
        required=True,
        help="Output S3 bucket name (substitutes ${OUTPUT_BUCKET}).",
    )
    p.add_argument(
        "--output-prefix",
        required=True,
        help="Output S3 prefix, trailing slash required (substitutes ${OUTPUT_PREFIX}).",
    )
    p.add_argument(
        "--account-id",
        required=True,
        help="12-digit AWS account id (substitutes ${ACCOUNT_ID}).",
    )
    p.add_argument(
        "--ecr-repo-arns",
        required=True,
        nargs="+",
        help="One or more ECR repository ARNs (substitutes ${ECR_REPO_ARNS_JSON_ARRAY}).",
    )
    p.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Optional output path; if omitted, the rendered JSON prints to stdout.",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    doc = render(
        template_path=args.template,
        input_bucket=args.input_bucket,
        output_bucket=args.output_bucket,
        output_prefix=args.output_prefix,
        ecr_repo_arns=args.ecr_repo_arns,
        account_id=args.account_id,
    )
    text = json.dumps(doc, indent=2, sort_keys=True) + "\n"
    if args.out is None:
        sys.stdout.write(text)
    else:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
