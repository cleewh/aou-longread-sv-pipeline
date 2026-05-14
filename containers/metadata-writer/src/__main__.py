"""Entry-point dispatcher for the metadata-writer container.

Usage (inside the container):

    docker run --rm <image> validate      [--manifest /path/to/manifest.json]
    docker run --rm <image> write         [--options ...]
    docker run --rm <image> compute-cost  [--options ...]

The dispatcher parses ``argv[1]`` as the subcommand and delegates to the
matching module's ``main()`` function. Subcommand-specific flags are
passed through verbatim as ``argv[2:]``.

Full implementations of each subcommand land in later tasks:
  - ``validate``      -> Task 3.2 (validator.py / Property 1)
  - ``write``         -> Task 3.8 (writer.py    / Property 5)
  - ``compute-cost``  -> Task 3.6 (cost_report.py / Property 19)

Requirements: 7.2, 12.2, 16.2, 17.10
Design: D8, MetadataWriter_Task contract
"""

from __future__ import annotations

import sys
from typing import Callable, Sequence

# Import the subcommand modules lazily-enough to keep cold-start cheap
# while still surfacing import errors at container startup rather than
# deep inside a HealthOmics task.
from . import cost_report, validator, writer

# Mapping from CLI subcommand -> module entry point. Each entry point
# accepts an optional ``argv`` list and returns an ``int`` exit code.
_SUBCOMMANDS: dict[str, Callable[[Sequence[str] | None], int]] = {
    "validate": validator.main,
    "write": writer.main,
    "compute-cost": cost_report.main,
}


def _print_usage(stream) -> None:
    names = ", ".join(sorted(_SUBCOMMANDS))
    print(
        f"usage: python -m metadata_writer <subcommand> [args...]\n"
        f"  subcommands: {names}",
        file=stream,
    )


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        _print_usage(sys.stderr)
        return 2
    subcommand, rest = argv[0], argv[1:]
    handler = _SUBCOMMANDS.get(subcommand)
    if handler is None:
        print(f"error: unknown subcommand {subcommand!r}", file=sys.stderr)
        _print_usage(sys.stderr)
        return 2
    return handler(rest)


if __name__ == "__main__":
    sys.exit(main())
