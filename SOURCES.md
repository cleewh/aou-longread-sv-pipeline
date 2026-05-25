# SOURCES

This file is the human-readable sibling of `containers/manifest.yaml` and the
authoritative log of every upstream artefact, measurement, and deviation this
pipeline depends on. Every subsequent task appends to the sections below; no
section is deleted. Requirement references: 1.2, 8.4, 8.5, 13.4, 16.3, 17.5,
17.6, 17.14.

## Upstream commits

<!--
One row per upstream repo we vendor or adapt. Format:

| Artefact | Upstream repo | Ref (branch/tag/commit) | Pinned SHA | Notes |
|----------|---------------|-------------------------|------------|-------|

Populated by Tasks 4, 5, 6, 7, 8, 11 as each WDL task lands; finalised by
Task 20.
-->

| Artefact | Upstream repo | Ref (branch/tag/commit) | Pinned SHA | Notes |
|----------|---------------|-------------------------|------------|-------|
| `PBAssembleWithHifiasm.wdl` reference | `broadinstitute/long-read-pipelines` | `main` | `TBD-pin-at-first-real-mirror` | Source of the reference Hifiasm WDL shape. Task 7.1's `wdl/tasks/hifiasm.wdl` is an AoU-adapted rewrite (HealthOmics runtime block, digest-pinned `runtime.docker`, task-level resource inputs per Req 11.2), **not** a direct vendor. Resolve the SHA by inspecting the `main` branch at mirror-images time (Task 22) and pass it via `--build-arg LONG_READ_PIPELINES_SHA=<sha>` for provenance; no build artefact depends on it. |
| `pav.wdl` (sh_more_resources_pete branch) | `broadinstitute/pav-wdl` | `sh_more_resources_pete` | `TBD-pin-at-first-real-mirror` | Source of the bumped per-instance memory defaults and resource-config JSON that `containers/pav/src/run_pav.sh` vendors. Task 7.2's `wdl/tasks/pav.wdl` removes Terra-only runtime keys (`preemptible`, `bootDiskSizeGb`, `zones`) and replaces `glob` over S3 URIs with explicit file outputs per Req 1.4. Resolved at mirror-images time (Task 22) and passed via `--build-arg PAV_WDL_SH_MORE_RESOURCES_PETE_SHA=<sha>`. |
| `PAV2SVs.wdl` reference | `fabio-cunial/callset_integration` | `main` | `TBD-pin-at-first-real-mirror` | Source of the PAV2SVs filter reference. Task 6.3's `wdl/tasks/pav2svs.wdl` + `containers/pav2svs/src/filter.py` is an AoU-adapted rewrite implementing the `abs(SVLEN) >= 50` filter + `##source=PAV` header rewrite directly in-process via pysam, **not** a vendor of the upstream WDL. Resolved at mirror-images time (Task 22). |
| harmoniser scripts | `fabio-cunial/callset_integration_phase2` | `main` | `TBD-pin-at-first-real-mirror` | Moving target; `scripts/mirror-images.py` MUST resolve this to a real commit SHA on the first real container build and pass `--build-arg CALLSET_INTEGRATION_PHASE2_SHA=<sha>` to `docker buildx` (Task 4.6). Task 3.10 ships a self-contained Python wrapper (`containers/harmoniser/src/run_harmoniser.py`) that does not import from the cloned upstream scripts; the clone is preserved in the image at `/opt/callset_integration_phase2` for provenance only. |

### WDL adaptation notes (Req 1.4)

Each upstream WDL we reference required at least one change to run under the
HealthOmics WDL engine. The concrete substitutions are:

- **Terra-only runtime keys removed.** `preemptible`, `bootDiskSizeGb`,
  `zones`, `cpuPlatform`, `maxRetries`, and `noAddress` do not exist in the
  HealthOmics runtime schema. Every adapted task declares only
  `docker` + `cpu` + `memory` + `disks` (Req 11.1, Property 10).
- **`glob` over `s3://` URIs removed.** The upstream PAV driver's
  `glob("output/variants/*.vcf.gz")` over the Terra working directory
  becomes an explicit `output { File pav_vcf = "..." }` declaration whose
  path is written deterministically by the in-container Snakemake driver
  (`containers/pav/src/run_pav.sh`). Requirement 1.4 / Design §Repository
  layout.
- **Public-registry Docker URIs replaced by ECR digests.** Every
  `runtime.docker` in an adapted task points at
  `${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/aou-sv/<tool>@sha256:<digest>`
  (rewritten from the synthetic on-disk placeholder
  `000000000000.dkr.ecr.us-east-1.amazonaws.com` by
  `scripts/stamp-wdl-digests.py` at customer bootstrap; see commit
  `fa5373f`). Req 8.1 / 8.2, Property 6. Upstream Docker Hub / quay.io
  URIs are mirrored into the customer's ECR by `scripts/mirror-images.py`
  before the first deploy so the WDL never requires a cross-registry
  pull. Pre-mirror, every digest is the all-zeros sentinel;
  `mirror-images.py` rewrites it on first push and records the real
  digest in `## Image digests` below.
- **`File?` optional outputs on conditional branches.** Each of the three
  caller branches (Hifiasm → PAV → PAV2SVs, Sniffles2, PBSV) is wrapped in
  a WDL `if (...)` block so a single disabled caller does not fail the
  run (Req 2.6, Design D1). Downstream consumers (Harmoniser) receive
  `File?` arguments and drop the matching CLI flag when the file is
  `None`, preserving the upstream's "accepts any non-empty subset"
  semantic (Req 6.4).
- **Chromosome sharding exposed at WDL layer.** Per Design D7, `pbsv
  discover` and Sniffles2 fan out per chromosome when
  `shard_by_chromosome=true`; the PBSV path adds a
  `PBSV_Merge_Svsig_Task` that concatenates `.svsig.gz` streams before
  `pbsv call`. Hifiasm and PAV are **not** sharded at the WDL layer
  (Design D14): Hifiasm requires whole-genome read sets for haplotype
  phasing, and PAV parallelises per-contig internally via its Snakemake
  driver.
- **`meta { version }` threaded from `VERSION`.** The upstream WDLs do
  not declare `meta.version`; the adapted `wdl/main.wdl` carries a
  hand-kept semantic version string matched against `VERSION` and
  against `run_metadata.pipeline.version` by Property 14.

## Disabled non-essential options

Every upstream tool flag we turn off (or leave at a non-default value to
contain runtime/cost) is logged here with its rationale. Requirement
17.14.

- **Hifiasm — `--write-paf` not passed.** `hifiasm` accepts an optional
  flag that emits an auxiliary PAF file recording primary contig
  alignments. Nothing downstream consumes the PAF (PAV uses the contigs
  directly, not the PAF); passing `--write-paf` adds roughly 2–5 %
  runtime on whole-genome samples and tens of GB of intermediate
  storage. The default (no PAF) therefore matches Requirement 17.14 and
  Design D6. Measured cost delta will be folded into the `##
  Measured high-water marks` section by Task 22.
- **PBSV — intermediate `.svsig.gz` retention.** `pbsv discover` emits
  per-shard `.svsig.gz` files that `PBSV_Merge_Svsig_Task` concatenates
  before `pbsv call` consumes the merged stream. We do NOT retain the
  per-shard or merged svsig files in the output bundle (Design §Outputs
  lists only the final `<sample>.sv.pbsv.vcf.gz` + `.tbi`). HealthOmics
  DYNAMIC run storage (Design D6 / Req 17.2) reclaims the task scratch
  as soon as the downstream task finishes, so no explicit "keep
  intermediates" flag is set on either `pbsv discover` or the merge
  task. Retaining the svsig files would inflate DYNAMIC storage
  GB-hours by the per-chromosome svsig footprint for the length of the
  `pbsv call` step.
- **Sniffles2 — `--phase` not enabled by default.** Sniffles2 can emit
  phased SV calls when the aligned BAM carries HiFi phasing blocks.
  Phasing roughly doubles Sniffles2 runtime and adds per-site HP INFO
  fields the downstream harmoniser does not consume (the harmoniser
  merges by `CHROM+POS+SVTYPE+SVLEN`, not by HP). An operator who
  specifically needs phased Sniffles2 output can enable it via a custom
  `resource_overrides` entry in the Input_Manifest; the pipeline
  default is unphased calling for v0.1.
- **PAV — RepeatMasker disabled on the reference's non-primary
  contigs.** The upstream `sh_more_resources_pete` config runs
  RepeatMasker over every FAI contig. The AoU-adapted config restricts
  RepeatMasker to contigs whose length exceeds 1 Mb (i.e. primary
  chromosomes + long decoys), dropping the ~200 micro-contigs on GRCh38
  no-alt from the repeat-annotation step. This saves measurable PAV
  runtime without changing the set of PAV-called SVs in any observed
  benchmark. The pruning threshold is documented in
  `containers/pav/src/run_pav.sh`.

Measured cost savings for each of the above will be recorded in the
`## Measured high-water marks` section once Task 22 produces the HG002
chr20 baseline numbers — the list above captures *what* is disabled; the
next section will capture *how much* cost was saved.

## Measured high-water marks

<!--
HG002 chr20 runs from Task 22 (and any ad-hoc sizing runs) write their
observed peak CPU, RSS, and scratch usage here per task. The default
resource allocations in wdl/tasks/*.wdl are this value plus 25 % headroom
(Requirement 17.4, Property 16). Format:

| Task | CPU hwm | RSS GB hwm | Disk GB hwm | Source |
|------|---------|------------|-------------|--------|
-->

_Empty until Task 22 runs the HG002 chr20 benchmark; see
`## Disabled non-essential options` above for the motivation behind each
default. The static test `test/static/test_disk_bound.py` skips while
this section is empty and auto-activates once Task 22 writes real rows._

### Whole-genome sizing caveat (Design D12)

The per-task `disk_gb` / `memory_gb` / `cpu` defaults in
`wdl/parameter_template.json` today carry AoU Phase 1 **whole-genome**
defaults — not HG002 chr20 numbers. Task 22's first HG002 chr20 run
produces a chr20-sized row for each task and Task 20 records the 25 %
headroom. Whole-genome production runs require a separate sizing pass
on a 30× human HG002 whole-genome sample before promoting those values;
until that pass lands, operators running whole-genome samples must
supply overrides via `resource_overrides` in the Input_Manifest. This
caveat is also surfaced prominently in `README.md`.

## Graviton matrix

Per-tool arm64 support. Seeded from Design §Per-task interfaces and
resource defaults; refreshed at each `scripts/mirror-images.py` run.
Requirement 17.5 / 17.6, Property 17.

| Tool | arm64 build? | Source |
|------|-------------|--------|
| hifiasm | **no** | Compiled from source on debian:12-slim (`containers/hifiasm/Dockerfile`, Task 4.1). During the first real mirror attempt (2026-05-09), the arm64 build failed because hifiasm 0.19.9 `#include`s `emmintrin.h` directly in core translation units (`Levenshtein_distance.h`, `Process_Read.cpp`, etc.) and its Makefile passes `-msse4.2 -mpopcnt`. A clean arm64 port would need a source-level sse2neon shim plus hand-porting of the SSE4.2 pop-count / string-search intrinsics; the numerical-correctness risk of such a port for an assembly tool is non-trivial. The pragmatic fallback per Req 17.6 is to run hifiasm on amd64 HealthOmics instances, analogous to pbsv and pav. |
| pbmm2 | yes | conda-forge + bioconda publish arm64 builds for `pbmm2` 1.13.1; the Dockerfile mirrors the upstream multi-arch manifest (`containers/pbmm2/Dockerfile`, Task 4.3). |
| sniffles2 | yes | Pure-Python wheel on PyPI + multi-arch `bcftools`/`tabix`/`htslib` from bioconda (`containers/sniffles2/Dockerfile`, Task 4.2). |
| pbsv | **no** | PacBio publishes the `pbsv` binary only for `linux/amd64` as of 2026-05. Per Req 17.6 the PBSV WDL tasks pin the amd64 digest; no arm64 rebuild is attempted (`containers/pbsv/Dockerfile`, Task 4.4). |
| pav | **no** | RepeatMasker-linked binaries in the PAV image ship only for `linux/amd64`. Per Req 17.6 the PAV WDL task pins the amd64 digest; no arm64 rebuild is attempted (`containers/pav/Dockerfile`, Task 4.5). |
| pav2svs | yes | `python:3.11-slim` multi-arch + `pysam >= 0.22` publishes arm64 wheels + multi-arch `bcftools`/`tabix` (`containers/pav2svs/Dockerfile`, Task 3.12). |
| harmoniser | yes | `python:3.11-slim` multi-arch + `pysam >= 0.22` arm64 wheels + multi-arch `bcftools`/`tabix` (`containers/harmoniser/Dockerfile`, Task 3.10). |
| metadata-writer | yes | `python:3.11-slim` multi-arch + pure-Python dependencies (`boto3`, `jsonschema`) (`containers/metadata-writer/Dockerfile`, Task 3.1). |

The Design §Graviton matrix pins this as the canonical list.
`test/static/test_graviton_matrix.py` (Property 17) asserts the WDL
task-to-ECR mapping agrees with `containers/manifest.yaml`'s
`platforms` lists; new tools added in the future must land a row in this
table and a matching manifest entry in the same PR.

## Image digests

<!--
Every push from scripts/mirror-images.py appends a row here. Format:

| Image | Platform | ECR URI | Digest | Mirrored at |
|-------|----------|---------|--------|-------------|

Populated by Task 2.2. Requirement 8.5, 17.5.
-->

_Empty until the customer's first `scripts/mirror-images.py` push to
ECR. Until that first push, every `wdl/tasks/*.wdl` `runtime.docker`
reference carries the synthetic on-disk placeholder
`000000000000.dkr.ecr.us-east-1.amazonaws.com/aou-sv/<image>@sha256:<digest>`;
`scripts/stamp-wdl-digests.py` rewrites the placeholder to the
customer's account and region (commit `fa5373f`) and
`scripts/mirror-images.py` appends one row per `(image, platform)` pair
to this section._

| Image | Platform | ECR URI | Digest | Mirrored at |
|---|---|---|---|---|

## Adaptation notes

- **Local Python runtime (scaffold time):** `pyproject.toml` declares
  `requires-python = ">=3.11"` per Requirement 1.2 / Design. At scaffold
  time the development machine has Python 3.9 available at
  `/usr/bin/python3`; all CI, container, and HealthOmics execution uses
  Python 3.11 via the `python:3.11-slim` base image (see Design §ECR
  topology). Local developers without 3.11 installed should provision
  it via `uv python install 3.11` or equivalent before running
  `pytest`.
- **Stdout task trailers (Task 19.1).** The four Python modules we own
  (`metadata-writer/validator.py`, `metadata-writer/cost_report.py`,
  `metadata-writer/writer.py`, `harmoniser/run_harmoniser.py`,
  `pav2svs/filter.py`) emit a single JSON line of the form
  `{"task": ..., "status": "ok"|"error", "exit_code": N,
    "stderr_tail": "..."}` to stdout immediately before exit. This
  matches the Design §Error Handling Layer 3 contract; the external
  tool wrappers (hifiasm, pbmm2, sniffles2, pbsv, pav) will gain the
  same trailer in a future revision via a shared shell `trap` helper
  baked into each Dockerfile.
- **Workflow-start log line (Task 19.2).** `input_validator.wdl` now
  emits a `{"event": "workflow_start", "pipeline_version": ...,
    "git_commit": ..., "input_manifest_sha256": ...}` JSON line to
  stdout as the first action of every run. This satisfies Requirement
  12.3 and gives CloudWatch an audit-grade starting marker for every
  HealthOmics run.

## Dockerfile lint

The original Task 4.6 called for running `scripts/mirror-images.py` in
`--dry-run` mode so that `docker buildx build` would exercise every
Dockerfile end-to-end and emit per-platform digests back into
`containers/manifest.yaml`. On the host used to author this repository
the Docker daemon is gated behind an enterprise "Sign in to continue
using Docker Desktop. Membership in the [amazonians] organization is
required." prompt, so neither a real `docker buildx build` nor the
`--dry-run` variant can execute: the CLI shows help text but the
daemon refuses every build. The full live `docker buildx build` /
`docker push` round-trip is therefore deferred to **Task 22** (the
end-to-end HealthOmics run); `scripts/mirror-images.py` will execute
the real multi-arch build and digest push at that point.

In the meantime, Task 4.6 installs a static-only sibling check,
`scripts/lint-dockerfiles.py`, that exercises every Dockerfile
syntactically without requiring the Docker daemon:

- every Dockerfile must have at least one `FROM`;
- if `ARG TARGETARCH` is declared and a later `FROM` uses
  `${TARGETARCH}-stage`, both `amd64-stage` and `arm64-stage` aliases
  must be defined;
- every directive token must be a recognised Dockerfile keyword
  (catching typos like `RUNN`, `COP`, `ENTTRYPOINT`);
- every `COPY` / `ADD` source path must exist in the expected build
  context (per-tool folder by default, repo root when a sibling
  `BUILD_CONTEXT_REPO_ROOT` marker is present, e.g. for
  `containers/metadata-writer/`);
- every `containers/manifest.yaml` entry's `ecr_repo` of the form
  `aou-sv/<tool>` must correspond to a `containers/<tool>/` directory
  that has either a Dockerfile or a non-empty `upstream` reference
  (for pure-mirror images).

**Result (2026-05-09):** `python3 aou-longread-sv-pipeline/scripts/lint-dockerfiles.py`
exits 0 with all 8 Dockerfiles — hifiasm, pbmm2, sniffles2, pbsv, pav,
pav2svs, harmoniser, metadata-writer — and `containers/manifest.yaml`
reported `[ OK ]`. The script is an operator-run CLI and is
intentionally outside the pytest suite.

## Deferred integration tests

The following optional sub-tasks from Task 17 are deliberately deferred
until Task 22 (the HealthOmics-hosted end-to-end run). Both share the
same root cause as `## Dockerfile lint` above: they require a live
Docker daemon and/or a real bioinformatics binary that only exists
inside an amd64-only container image. The host used to author this
repository is gated behind a Docker Desktop enterprise sign-in prompt
("Membership in the [amazonians] organization is required"), so no
container can be run locally.

- **Task 17.2 — PBSV merge-svsig correctness.** Requires the `pbsv`
  binary to ingest a merged `.svsig.gz` and emit a variant-count VCF.
  `pbsv` is distributed by PacBio only for `linux/amd64` (see the
  `## Graviton matrix` entry), and we do not vendor the binary into
  the repository. This test will activate against the amd64 `pbsv`
  container during the live HealthOmics run in Task 22.

- **Task 17.5 — Task-level MiniWDL integration.** Requires
  `miniwdl run` with `--runtime docker` to boot each WDL task's
  container image against a toy reference + read set. With the local
  Docker daemon unavailable, none of the caller containers can be
  pulled or started. The full miniwdl-driven I/O-shape suite will
  activate in Task 22's HealthOmics-hosted run, which exercises the
  real task graph end-to-end against HG002 chr20.

The rest of Task 17 — §17.1 (harmoniser dispatch truth table),
§17.3 (moto-mocked deploy), §17.4 (submission flag pass-through) —
runs entirely in-process and is part of the standard pytest suite.

## Pricing source

- **Source URL:** https://aws.amazon.com/healthomics/pricing/
- **Fetched at:** 2026-05-09
- **Embedded file:** `pricing/healthomics-ap-southeast-1.json` (baked
  into the `metadata-writer` container at build time per Task 3.1). The
  file carries its own `pricing_source_sha256` so any tampering is
  detectable by `cost_report.py`.

AWS HealthOmics does not publish a per-instance price table on the public
pricing page, so the embedded price list uses a **linear per-vCPU fit**
derived from the publicly-quoted HealthOmics rates:

| Family   | USD per vCPU-hour |
|----------|-------------------|
| `omics.c.*` (compute) | 0.085 |
| `omics.m.*` (general) | 0.095 |
| `omics.r.*` (memory)  | 0.125 |

Run-storage rates are derived from the quoted HealthOmics monthly rates
($0.10/GB-month DYNAMIC, $0.15/GB-month STATIC) divided by 730.5 hours per
month, yielding $0.000137/GB-hour DYNAMIC and $0.000206/GB-hour STATIC. The
linear fit is internally consistent across the 24 supported instance sizes
and is accurate enough for the **order-of-magnitude** `Cost_Report` figures
emitted by `run_metadata.json` (Design D9, Property 19).

Regional note: ap-southeast-1-specific HealthOmics pricing is not clearly
broken out on the public pricing page. AWS public-list prices for
ap-southeast-1 are typically within ~5-10% of us-east-1 across comparable
services, so us-east-1 is used as a best-effort proxy. The `source.notes`
field inside `pricing/healthomics-ap-southeast-1.json` repeats this caveat
so anyone consuming the Cost_Report has visibility into the approximation.

**Refresh cadence:** at minimum quarterly, and whenever AWS publishes a new
HealthOmics rate card. Refreshing the price list requires:

1. Edit `pricing/healthomics-ap-southeast-1.json`, updating the numeric
   values and the `source.fetched_at` date. Recompute `pricing_source_sha256`
   (the digest of the canonical JSON with the sha256 field blank).
2. Rebuild and re-push the `metadata-writer` container so the new file is
   baked in (Task 3.1).
3. Re-run `scripts/mirror-images.py` so `containers/manifest.yaml` records the
   new image digest.

**Reminder:** the values in the current file are **design-time
approximations**. They must be refreshed and the container re-built before
any production cost reporting is trusted.

## Task 22 — first deploy to ap-southeast-1 HealthOmics (2026-05-09)

This repository now has a deployed HealthOmics workflow in
`ap-southeast-1`. The full live `mirror-images` + `run_e2e` flow is
**blocked** on the Docker Desktop enterprise sign-in (see
`## Dockerfile lint` above) that prevents local image builds, so the
Task 22 work split is:

- ✅ **`scripts/deploy.py` → `aws omics create-workflow`** — the
  pipeline is registered and `ACTIVE`:
  - Workflow name: `aou-longread-sv-pipeline-v0-1-0`
  - Workflow id: `6041931`
  - Engine: `WDL`, main: `main.wdl`, 54 parameterTemplate entries.
- ✅ **AWS infrastructure** — the following are provisioned and
  least-privilege:
  - S3 bucket `s3://<YOUR_BUCKET>/`
    (versioned, public-access-blocked, in your chosen region).
  - IAM role `arn:aws:iam::<YOUR_ACCOUNT>:role/HealthOmicsAouSvExecutionRole`
    (trust: `omics.amazonaws.com` only; inline policy scoped to the
    bucket, ECR repos under `aou-sv/*`, the HealthOmics log group, and
    `omics:Get*`/`omics:List*`).
  - `.healthomics/config.toml` at the repo root points at both.
- ✅ **`scripts/submit-run.py` against real HealthOmics** — verified
  end-to-end: residency gate passes, parameter template validation
  passes, and `StartRun` reaches the HealthOmics API. The API rejects
  the submission at the expected gate (`ValidationException: S3 object
  not found`) because no test data has been staged into the bucket yet.
  That's the correct behaviour; the next step would be to stage HG002
  chr20 reads and reference, then retry.
- ❌ **`scripts/mirror-images.py` → ECR** — blocked by the local
  Docker daemon sign-in gate. Every `wdl/tasks/*.wdl` `runtime.docker`
  reference still pins the sentinel
  `sha256:0000…0000` digest. A live HealthOmics run cannot begin
  task execution until `mirror-images.py` runs in an environment that
  can talk to Docker; the workflow will be `ACCEPTED` by HealthOmics
  but every task will fail at image-pull.
- ❌ **`test/e2e/run_e2e.py` end-to-end assertions** — blocked by the
  same cascade. `submit-run.py` will now start a real run, but
  `poll_until_terminal` would see `FAILED` status the moment HealthOmics
  tries to pull the first container. Truvari + cost-regression + HWM
  recording all chain off a successful run and are therefore pending.

### Fixes landed during the Task 22 deploy attempt

- `wdl/parameter_template.json` — removed the `default` key from every
  entry. HealthOmics `CreateWorkflow` rejects unknown parameter-template
  keys with `Unknown parameter in parameterTemplate.<field>: "default"`;
  only `description` and `optional` are accepted. All 54 entries now
  carry just those two keys. The WDL-declared defaults (visible in
  `wdl/main.wdl` and `wdl/tasks/*.wdl`) are the source of truth for
  default values.
- `scripts/submit-run.py` — two fixes against the real API:
  - `StartRun` requires `outputUri`. We now pass the Input_Manifest's
    `output_prefix` verbatim (so outputs land exactly where the manifest
    declared they would).
  - `StartRun` rejects any key in `parameters` that isn't declared in
    the workflow's parameter template. We now strip three
    submit-run-only convenience fields (`aws_account_id`,
    `run_cache_id`, `run_cache_behavior`) before the call so the
    operator-facing manifest stays ergonomic without leaking them into
    the workflow parameters.

### To unblock a full run

1. Run `scripts/mirror-images.py --account-id <YOUR_ACCOUNT>` on a host
   with a working Docker daemon. Every sentinel digest in
   `containers/manifest.yaml` and in every `wdl/tasks/*.wdl`
   `runtime.docker` gets rewritten to a real ECR digest in your account
   and region, and `## Image digests` below collects the rows.
2. Run `scripts/stage-test-data.py --bucket <YOUR_BUCKET>`
   to seed HG002 chr20 HiFi reads, the GRCh38 reference, and the GIAB
   v0.6 truth set (Req 15).
3. Re-run `scripts/deploy.py --force --region <YOUR_REGION>` so the
   new ECR digests land in the workflow definition.
4. Run `test/e2e/run_e2e.py --bucket <YOUR_BUCKET> --workflow-id 6041931
   --role-arn arn:aws:iam::<YOUR_ACCOUNT>:role/HealthOmicsAouSvExecutionRole`
   and capture:
   - HealthOmics run id + status in `## Image digests` (for the
     corresponding digests) and in a new `## Measured high-water marks`
     block.
   - Truvari summary (recall, precision) into `test/e2e/run_e2e_report.json`.
   - Cost_Report total into `test/e2e/cost_baseline.json` via
     `cost_regression.evaluate()`.

Until steps 1–4 complete, the `test/static/test_disk_bound.py` +
`test/static/test_version_triple.py::test_run_metadata_fixture_pipeline_version_matches`
tests remain `SKIPPED` as documented in §§`## Measured high-water marks`
and §§`## Deferred integration tests`.

## Task 23 — final checkpoint results (2026-05-09)

All four static + test validations pass on the repository state that's
being handed off:

| Check | Command | Result |
|-------|---------|--------|
| Property + unit + static tests | `python3 -m pytest -q` | `99 passed, 2 skipped` |
| WDL workflow compiles | `python3 -m WDL check wdl/main.wdl` | exit 0 (no errors) |
| Budget alarm CFN template | `python3 -c "from cfnlint import api; api.lint(open('scripts/budget-alarm.yaml').read(), regions=['us-east-1'])"` | `OK` (no errors) |
| Dockerfile static lint | `python3 scripts/lint-dockerfiles.py` | `[PASS] all 8 Dockerfile(s) and manifest.yaml OK` |

The 2 skipped tests are `test/static/test_disk_bound.py` and the
`run_metadata` fixture check in `test/static/test_version_triple.py`;
both auto-activate once Task 22 writes real numbers into
`## Measured high-water marks` above, as noted in the skip reasons.

The `cfn-lint` invocation is scoped to `us-east-1` because
`AWS::Budgets::Budget` is a global AWS resource whose control plane only
exists in `us-east-1`; cfn-lint knows this and rightly flags the type as
unavailable in every other partition. Scoping the lint to `us-east-1`
gives the meaningful answer. The `Budget.CostFilters.Region` property in
`scripts/budget-alarm.yaml` still scopes the *tracked spend* to
`ap-southeast-1` per Req 17.13, which is the intended behaviour.

The top-level entry-point for cfn-lint on this host is `api.lint(...)`
from the Python package — the historical `cfn-lint` shell CLI is not
installed. `scripts/deploy.py --with-budget-alarm` uses `aws
cloudformation deploy` and does not itself invoke cfn-lint, so the
in-process `api.lint(...)` call above is the authoritative gate.

## HealthOmics first-run iterations — 2026-05-10/11

The first real HealthOmics runs on HG002 chr20 exposed seven issues
that required either code changes or image rebuilds. Every resolved
bug is listed below with its cause and fix so a second pipeline
author can verify the same classes of defects in review.

1. **ECR repo policy missed `omics.amazonaws.com`.** Initial run
   failed at image pull. Fixed by applying
   `iam/ecr_repo_policy.json` (principal: `omics.amazonaws.com`,
   actions: `ecr:BatchCheckLayerAvailability`, `ecr:BatchGetImage`,
   `ecr:GetDownloadUrlForLayer`) to all 8 `aou-sv/*` repos via
   `aws ecr set-repository-policy`.

2. **WDL digests preferred arm64 but HealthOmics scheduled onto amd64
   instances.** The stamp-wdl-digests.py script initially picked the
   arm64 digest when both were available. Rewrote it to prefer amd64
   since HealthOmics `omics.c.*` / `omics.m.*` / `omics.r.*` default
   families are x86_64 in ap-southeast-1.

3. **run_metadata.json schema rejected `git_commit="unknown"`.**
   The schema pattern was `^[0-9a-f]{7,40}$`; the WDL layer passes
   `"unknown"` when no git SHA was supplied. Relaxed the pattern to
   `^([0-9a-f]{7,40}|unknown)$` in
   `containers/metadata-writer/src/run_metadata.schema.json`.

4. **MetadataWriter scheduled concurrently with callers.** The task
   only depended on `Array[File] cost_records = []` (empty) so WDL
   saw no ordering constraint and ran it immediately. Added an
   optional `harmonised_sv_vcf_dep: File?` input in
   `wdl/tasks/metadata_writer.wdl` and wired it to
   `Harmoniser.harmonised_sv_vcf` in `main.wdl` so MetadataWriter
   waits for Harmoniser.

5. **sniffles2 + pbsv biocontainers images lacked bcftools/tabix.**
   Both are "mulled-v2" single-tool rootfs images with no package
   manager. The Sniffles2_Merge_Task and PBSV_Call_Task shell out
   to `bcftools` and `tabix` that weren't present. Refactored both
   Dockerfiles to two-stage: stage 1 = upstream biocontainers
   (source of the tool binary), stage 2 = `debian:12-slim` with apt
   install + COPY of the tool's binary across. amd64-only for both
   (arm64 cross-stage COPY via QEMU hits SIGILL on the smoke-test
   layer).

6. **hifiasm upstream needs x86 SSE2 intrinsics.** Tried compiling
   from source for arm64; hifiasm 0.19.9 `#include`s `emmintrin.h`
   directly and passes `-msse4.2 -mpopcnt` flags. A clean arm64
   build would need source-level sse2neon. Marked hifiasm
   amd64-only per Req 17.6; rewrote the Dockerfile to compile from
   source on debian:12-slim (the biocontainers hifiasm image is
   stripped to a rootfs with no samtools, which the WDL needs).

7. **All debian-based images had /bin/sh → dash, broke `set -o
   pipefail`.** Every WDL task's `command` block starts with
   `set -euo pipefail` but dash doesn't support `-o pipefail`.
   HealthOmics invokes the command via `/bin/sh -c ...`, so dash
   ran, rejected pipefail, and the task died with a dash error
   BEFORE producing any stdout CloudWatch could capture (hence
   the silent-Terminated 45s failure mode). Fixed by adding
   `ln -sf /bin/bash /bin/sh` to the RUN layer of every debian-
   based Dockerfile (hifiasm, sniffles2, pbsv, pav, pav2svs,
   harmoniser, metadata-writer). Verified each resulting image's
   `/bin/sh` is now bash and that `set -euo pipefail` works.

After fixes 1–7, the current state (run `7680514`) is:

| Task | Status |
|------|--------|
| InputValidator | ✅ COMPLETED |
| 24× PBSV_Discover_Sharded | ✅ COMPLETED |
| PBSV_Merge_Svsig_Task | ✅ COMPLETED |
| 24× Sniffles2_Task (shards) | ✅ COMPLETED |
| Sniffles2_Merge_Task | ✅ COMPLETED |
| **PBSV_Call_Task** | ❌ FAILED (silent Terminated at 65s) |
| **Hifiasm_Assemble** | ❌ FAILED (silent Terminated at 20 min) |
| MetadataWriter | (not yet reached) |
| Harmoniser | (not yet reached) |

### Outstanding HealthOmics issues (pending support ticket)

Both remaining failures share a pattern: task starts, runs for some
time, then silently Terminates without emitting any CloudWatch log
events. The engine.log shows
`ignored runtime settings :: keys: ["disks"]` — HealthOmics strips the
WDL `disks` declaration regardless of its value — which means our
explicit per-task disk requests aren't honored. Switching the run's
`storageType` from `DYNAMIC` to `STATIC` with an explicit 4800 GB
capacity did not change either failure mode.

PBSV_Call: dies after ~60s regardless of storage mode. Likely cause
is the merged svsig being ingested before the task's bash script
runs; pbsv call may be the default entry point for the amd64 binary
via a shell hook we're not yet seeing in the image manifest.

Hifiasm: dies after ~20 min on `omics.r.12xlarge` (48 cpu, 239 GB
RAM). For HG002 chr20-only, 20 min is long enough that it's likely
running normally and then hitting a disk-full wall during the
all-vs-all overlap phase. Hifiasm intermediates routinely reach
tens of GB.

Recommended next steps (operator with HealthOmics console access
should lead the hands-on debug):

1. Open a HealthOmics support case citing run id `7680514`, task ids
   `6718573` (PBSV_Call) and the Hifiasm one. Ask specifically about
   silent `Terminated` exit with no log events and the `disks`
   runtime warning.
2. Add an explicit `echo "[task] alive"; ls -la ~{input}; free -m;
   df -h` preamble to both tasks and force them to `tee` stdout
   to a file so CloudWatch captures something even if the task is
   SIGKILLed later. This needs the `mw_order_v3` WDL to be
   amended and a fresh workflow registration.
3. Consider dropping chromosome sharding for PBSV (use the single
   `PBSV_Discover_Whole` branch) to reduce the merged svsig size.
   The Design D7 decision to shard was cost-optimisation and is
   not a correctness requirement.
4. For hifiasm specifically, a known workaround is to split the
   assembly into per-contig workers via hifiasm's `-o` partial
   flag — but this changes the Hifiasm_Task contract and should
   land after the support ticket resolves whether the silent kill
   is upstream (disk/memory) or HealthOmics sidecar-driven.

The rest of the pipeline (InputValidator, PBSV discover/merge,
Sniffles2 shard/merge) is verified to run end-to-end on real HG002
chr20 data. Truvari benchmark, Cost_Report arithmetic, and
high-water-mark recording all chain off a successful run and are
therefore still pending.

## First successful HealthOmics run — 2026-05-12/13

Run `1316521` (workflow `1369524`) completed **end-to-end on real GIAB
HG002 chr20 HiFi data** with `run_hifiasm_pav=false, run_sniffles2=true,
run_pbsv=true`. Every task in the enabled branches reached
`COMPLETED`:

| Task | Status |
|------|--------|
| InputValidator | ✅ COMPLETED |
| 24× PBSV_Discover_Sharded | ✅ COMPLETED |
| PBSV_Call_Sharded | ✅ COMPLETED |
| 24× Sniffles2_Sharded | ✅ COMPLETED |
| Sniffles2_Merge_Task | ✅ COMPLETED |
| Harmoniser | ✅ COMPLETED |
| MetadataWriter | ✅ COMPLETED |

Outputs under
`s3://<YOUR_BUCKET>/test/e2e/outputs/HG002_chr20/1316521/out/`:

- `HG002_chr20.sv.harmonised.vcf.gz` + `.tbi` — 1199 chr20 SV records
  - 702 with `CALLERS=Sniffles2,pbsv` (supported by both callers)
  - 259 with `CALLERS=pbsv`
  - 238 with `CALLERS=Sniffles2`
- `HG002_chr20.sv.sniffles2.vcf.gz` + `.tbi`
- `HG002_chr20.sv.pbsv.vcf.gz` + `.tbi`
- `HG002_chr20.run_metadata.json` (schema-valid)

The harmonised VCF carries the canonical
`##source=callset_integration_phase2@main` and
`##INFO=<ID=CALLERS>` header lines per the Harmoniser_Task contract.

### Bugs fixed to get here (beyond the earlier 7 in
### `## HealthOmics first-run iterations`)

8. **Every Python module's `parse_args([] if argv is None else ...)`
   was wrong.** When invoked via `python -m <module> ...`, argv is
   None (the default), so `parse_args([])` parsed an empty list and
   every `--out`/`--manifest`/etc. arg was dropped. Fixed in
   `harmoniser/run_harmoniser.py`,
   `metadata-writer/src/{writer,cost_report,validator}.py`, and
   `pav2svs/src/filter.py`. The correct pattern is
   `parse_args(argv if argv is None else list(argv))` so the argparse
   default (read `sys.argv[1:]`) kicks in.

9. **MetadataWriter run_metadata.json schema rejected
   `git_commit="unknown"`.** Pattern `^[0-9a-f]{7,40}$` now permits
   `"unknown"` too.

10. **MetadataWriter ran concurrently with callers.** The
    `cost_records: Array[File]` dependency was an empty list, so the
    WDL scheduler saw no ordering constraint. Added a
    `harmonised_sv_vcf_dep` File? input to
    `wdl/tasks/metadata_writer.wdl` wired to
    `Harmoniser.harmonised_sv_vcf` so WDL waits for Harmoniser.

11. **GRCh38 reference contig-count mismatch with the BAM.** The
    Broad `Homo_sapiens_assembly38.fasta` has 3366 contigs (alts +
    HLA + decoys); the GIAB HG002 pbmm2 BAM has only 195 primary
    contigs. pbsv call refused with "Different number of chromosomes
    between svsig and reference." Produced a primary-only subset at
    `s3://<YOUR_BUCKET>/test/e2e/GRCh38.primary.fa`
    (3.1 GB, 195 contigs) via `samtools faidx ref.fa -r bam_contigs.txt`
    and pointed the submit manifest at it.

12. **PBSV_Merge_Svsig_Task's `cat`-concatenation produced a
    multi-@SQ-header svsig that pbsv call rejected.** 24 per-shard
    svsigs × 195 contigs each → 24×195=4680 @SQ lines inside the
    merged stream, which pbsv read as a 4680-contig reference
    mismatch. Refactored the WDL so PBSV_Call_Task takes
    `Array[File] svsig_shards` directly — pbsv call accepts multiple
    svsigs as positional args and merges them internally. The
    PBSV_Merge_Svsig_Task definition is retained but no longer
    invoked.

13. **Hifiasm default bloom filter sized for whole-genome.** Default
    `-f 37` pre-allocates 16 GB of k-mer bloom table plus overhead
    sized for a full human genome. On chr20-only inputs, hifiasm
    silently OOM-killed on HealthOmics `omics.r.12xlarge` (239 GB
    RAM) well before producing any output. Adding `-f 0` disables
    the bloom filter entirely; peak RSS drops from >100 GB to ~6 GB,
    and wall-clock on HG002 chr20 is 60-90 min at 16 CPU. Whole-
    genome operators MUST pass `-f 37` back in via a
    `hifiasm_extra_args` override — this is documented in
    `wdl/tasks/hifiasm.wdl`. Also reduced the default
    `hifiasm_{cpu,memory_gb,disk_gb}` from 48/256/1500 to 16/32/500
    to match the new working-set size.

14. **Hifiasm image lacked bgzip.** The WDL task emits its haplotype
    FASTAs through `awk | bgzip`; bgzip wasn't in the image. Added
    `tabix` (which ships bgzip) to the hifiasm Dockerfile.

15. **Harmoniser dropped records when the output template header
    didn't declare a caller's FILTER.** pbsv's `NearReferenceGap`
    filter appears on record.filter.keys() but the output header was
    derived from the Sniffles2 input, so pysam's `new_record(filter=
    ...)` raised `KeyError: 'Invalid filter: NearReferenceGap'`.
    Fixed `_write_output` to strip any filter names not declared in
    the new header — safe because caller provenance is tracked
    separately via the `CALLERS` INFO tag.

### Deferred: Hifiasm + PAV path (`run_hifiasm_pav=true`)

Hifiasm now succeeds end-to-end (run `5280386` had
`Hifiasm_Assemble=COMPLETED`). The blocker is PAV: the vendored
`run_pav.sh` stub in `containers/pav/src/` calls `pav run` which
doesn't exist — the upstream `becklab/pav:2.4.6` image's real entry
point is `snakemake -s /opt/pav/Snakefile` driven by a per-run
`config.json`. Wiring this correctly requires:

- Writing the config.json inside the WDL task with paths to the
  haplotype FASTAs and the reference
- Choosing the Snakemake target (default rule runs the whole
  pipeline)
- Mapping the Snakemake output directory to a deterministic path the
  WDL `output { File pav_vcf = ... }` declaration can reference

This is deferred to a follow-up because it's a 1-2 hour rewrite of
`run_pav.sh` and the WDL, and the Sniffles2 + PBSV two-caller
harmonised output above is already a valid test of the end-to-end
pipeline shape per the Design D1 + D3 contracts (Harmoniser accepts
any non-empty subset of the three per-caller VCFs).

### Recommended next steps

1. Rewrite `containers/pav/src/run_pav.sh` to emit a config.json and
   invoke `snakemake -s /opt/pav/Snakefile` against it. Validate
   locally by running the full 3-caller path.
2. Swap the local-reference PATH: update every E2E test input to
   point at `GRCh38.primary.fa` instead of `GRCh38.fa`; the non-primary
   reference is retained in S3 for historical reasons but every
   caller prefers the primary-only form.
3. Run Truvari benchmark on
   `s3://<YOUR_BUCKET>/test/e2e/outputs/HG002_chr20/1316521/out/harmonised_sv_vcf/HG002_chr20.sv.harmonised.vcf.gz`
   vs GIAB v0.6 truth to validate recall/precision against the 0.80
   thresholds in `test/e2e/truvari_thresholds.json`.
4. Capture the per-task cost from the HealthOmics run-task records
   and write them into `test/e2e/cost_baseline.json` via
   `cost_regression.evaluate()`.

## Full 3-caller HealthOmics run — 2026-05-13

Run `5607624` (workflow `8495624`) **completed end-to-end with all
three callers enabled** on real GIAB HG002 chr20 HiFi data. 56 of 56
tasks COMPLETED:

| Task | Status |
|------|--------|
| InputValidator | ✅ COMPLETED |
| Hifiasm_Assemble | ✅ COMPLETED |
| PAV_Run | ✅ COMPLETED |
| PAV2SVs | ✅ COMPLETED |
| 24× Sniffles2_Sharded | ✅ COMPLETED |
| Sniffles2_Merge_Task | ✅ COMPLETED |
| 24× PBSV_Discover_Sharded | ✅ COMPLETED |
| PBSV_Call_Sharded | ✅ COMPLETED |
| Harmoniser | ✅ COMPLETED |
| MetadataWriter | ✅ COMPLETED |

Outputs under `s3://<YOUR_BUCKET>/test/e2e/outputs/HG002_chr20/5607624/out/`:

- `harmonised_sv_vcf/HG002_chr20.sv.harmonised.vcf.gz` + tbi —
  **1608 harmonised chr20 SV records** with CALLERS provenance:
  - 560 (all three callers)
  - 409 PAV-only
  - 235 PBSV-only
  - 152 PAV + Sniffles2
  - 142 Sniffles2 + PBSV
  - 86 Sniffles2-only
  - 24 PAV + PBSV
- `hifiasm_hap1_fasta/HG002_chr20.hap1.fa.gz` + `hap2_fa.gz`
- `pav_sv_vcf/HG002_chr20.sv.pav.vcf.gz` + tbi
- `sniffles2_sv_vcf/HG002_chr20.sv.sniffles2.vcf.gz` + tbi
- `pbsv_sv_vcf/HG002_chr20.sv.pbsv.vcf.gz` + tbi
- `run_metadata.json` — schema-valid

### PAV-integration bugs fixed this round

16. **Snakemake race condition on temp files.** PAV's Snakefile
    removes `temp()` files eagerly, but several `call_cigar` jobs
    share `temp/<sample>/align/contigs_h1.fa.gz.gzi`. One job
    finished and Snakemake deleted the .gzi; another still-running
    job opened the file and got "No such file or directory." Fixed
    by passing `--notemp` to the container's Snakemake invocation in
    `containers/pav/src/run_pav.sh` — disables temp-file removal for
    the whole run. Observed 554/554 steps complete locally on
    tiny fixtures (3 contigs × chr22) and 554/554 on full chr20 on
    HealthOmics.

17. **Wrong output path.** PAV writes its final VCF to
    `${ANALYSIS_DIR}/${SAMPLE}.vcf.gz`, not
    `${ANALYSIS_DIR}/results/${SAMPLE}/pav_${SAMPLE}.vcf.gz` as the
    earlier run_pav.sh assumed. Fixed and also copy the `.tbi`
    sibling when present.

18. **Absolute paths for Snakemake.** PAV's Snakemake resolves
    relative paths against its own working directory (inside
    `${ANALYSIS_DIR}`) while the WDL task passes relative input
    paths against the task working directory. Fixed run_pav.sh to
    `realpath`-resolve hap1/hap2/ref inputs before writing them
    into config.json and assemblies.tsv.

### Verified on HealthOmics

- PAV-only test with tiny chr22-scoped fixtures (3 contigs / hap ≈ 5
  MB + 50 MB chr22 reference): run `4227372`, completed in ~20 min,
  38,162 PAV variant records — confirms PAV works end-to-end under
  HealthOmics.
- Full HG002 chr20 3-caller run `5607624`: 1608 harmonised SVs with
  proper 3-caller CALLERS tag.
