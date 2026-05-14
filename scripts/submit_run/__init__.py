"""submit_run package — pure-logic helpers for scripts/submit-run.py.

Each submodule encapsulates one of the Layer 1 pre-flight checks that the
submission CLI runs before calling ``aws omics start-run``:

* :mod:`submit_run.residency` — Property 9 region/ECR residency gate.
* :mod:`submit_run.resources` — Property 11 resource-override resolver.
* :mod:`submit_run.shard_planner` — Property 18 chromosome shard planner.
* :mod:`submit_run.instance_selector` — Property 15 cost-optimal instance picker.

The CLI wrapper lives at ``scripts/submit-run.py`` (hyphenated per
Requirement 10.1 / Design §submit-run pseudocode); it inserts ``scripts/``
on ``sys.path`` so this package imports cleanly as ``submit_run`` despite
the hyphen in the wrapper filename.
"""
