# Research projects: the `research` kind

A repository with only a `dataset` and/or a `task-runner` component is a
**research project**: no container image, no Lichess bot, no website. It gets
what a bot project gets for tasks and data (a network, launch templates,
instance roles, a log group, a task results bucket, dataset buckets) and
nothing else. The first intended consumer is `math-prover`, which fine-tunes
open-weight models on a GPU and checks proofs afterwards. It does not exist
yet, so everything here is written from the code and the tests, and the parts
no real run has exercised are listed under [Not yet proven](#not-yet-proven).

The kind is chosen from the components: at least one `dataset` or
`task-runner`, and neither `container-image` nor `lichess-bot`. A file with
exactly one of those two is an error, so a bot project cannot become a
research project by losing a component. The bot-only commands (`bot push`,
`bot deploy`, `bot reconcile`, `bot secrets`, `bot status`) refuse a research
project. A research project without a task runner creates no VPC.

The commands `validate`, `plan`, `apply`, `destroy`, `task ...` and
`cost report` work on both kinds. See [remote-tasks.md](remote-tasks.md),
[datasets.md](datasets.md) and [costs.md](costs.md) for the parts they share
with bot projects.

## Central guardrails

What a project may ask for is decided in this repository, in `config.yaml`,
never in the project's own file:

```yaml
tasks:
  limits: { max_hours: 6, max_vcpus: 256, root_volume_gb: 200 }
  max_run_cost_usd: 50

projects:
  rusty-chess:
    instance_types: [c8a.xlarge, ...]
    task_profiles: [cpu, gpu, sprt, match, datagen, relabel]
    datasets:
      jwall-engineering-rusty-chess-nnue: {}
```

`foundation/bots.tf` reads `projects` for the launch role's allow-lists and
the dataset policies. `task run` and `plan` cross-check the project file
against it and refuse a file that asks for more: an instance type, profile or
bucket that is not listed, a limit above `tasks.limits`, or retention that
differs from the entry (see [Retention](#retention-is-approved-in-configyaml)).

Which version of `config.yaml` applies depends on where the check runs:

| Check | Takes effect |
|---|---|
| IAM allow-lists (instance types, profiles, dataset policies) | at the foundation apply, after the release |
| `task run` checks (cost ceiling, limits, cross-checks) | when the consumer bumps its pinned tool version, because `remote-task.yml` reads `config.yaml` from that version |
| **Bot apply** (retention and allow-list checks) | at once: it runs this repository's current code |

### Changing a guardrail

1. A pull request here that changes `config.yaml` (and `foundation/bots.tf`
   if the registry changes). A reviewer reads it as a decision.
2. A release. The foundation apply updates the IAM allow-lists.
3. A pin bump in the consumer, so its `task run` uses the new checks.

A consumer that stays on an older pin skips the newer CLI checks. IAM still
bounds it, because the allow-lists are enforced by the launch role, not by the
CLI.

`task list`, `task terminate` and `task sweep` skip these checks, so an
instance can always be stopped after a guardrail change. Every other load error
still applies to them. `plan`, `apply`, `destroy` and `task run` keep the
checks, so **destroy a project before removing its central entry**; see
[Destroy](#destroy).

## Onboarding a research project

The repository must exist first, because the registry needs its numeric
repository ID (`gh api repos/<owner>/<repo> --jq .id`). Then, in this
repository:

1. Add the project to `local.bots` in `foundation/bots.tf`, and to
   `local.research_projects` there, which gives it `compute/*` instead of
   `container/*` and no release role.
2. Add its `projects:` entry to `config.yaml`: instance types, task profiles,
   and each dataset bucket with its retention. Keep
   `tests/fixtures/config.yaml` identical to it.
3. Add it to the CI matrices: `bot-plan` in `.github/workflows/ci.yml` and
   the `project` options in `.github/workflows/bot-apply.yml`.
4. Release. The foundation apply creates the project's roles and grants.

Then, in the consumer and by the owner:

5. Merge the project file (`jwall-engineering-sites.yaml`) to the consumer's
   default branch. **Bot apply** reads it from there.
6. Run **Bot apply** for the project. It creates the buckets, launch
   templates, roles, network and log group.
7. Raise the GPU quota if needed. On 2026-11-02 the account had 5 on-demand
   G/VT vCPUs and 0 G/VT spot vCPUs, so one `g6e.xlarge` (4 vCPUs) at a time,
   and no `g6e.2xlarge` (8). See [remote-tasks.md](remote-tasks.md#quotas).
   Keep the profile's `max_vcpus` within the quota.
8. Run a GPU smoke test first (see [Not yet proven](#not-yet-proven)).

**Project names.** The project's roles are `<org>-<project>-...`, and the
policies match them as `<org>-<project>-*`. A project named `math` would
therefore match the roles of `math-prover`. Avoid project names that are
prefixes of each other.

**Versioning order.** S3 advises waiting 15 minutes after enabling versioning
on a bucket before writing to it. A research project's apply must also follow
the foundation apply that grants `s3:PutBucketVersioning` to `dataset/manage`;
before that, the apply role is refused when it turns versioning on.

## A sample project file

The file below is `tests/fixtures/bots/math-prover.yaml`, which the tests load
and render. It is a proposal: the project is not registered, so its central
entry exists only in the tests' `research` fixture.

```yaml
version: 1
project: math-prover
repository: jwallace145/math-prover

tags:
  Organization: jwall-engineering
  Project: math-prover
  Environment: prod
  Repository: jwallace145/math-prover
  Owner: jwallace145

components:
  - kind: dataset
    name: artifacts
    bucket: jwall-engineering-math-prover-artifacts
    prefix: data/
    versioning: true
    expire_noncurrent_days: 90
    expire_prefixes: { tmp/: 30 }

  - kind: task-runner
    rust: false                     # no Rust toolchain on the instances
    instance_types: [c8a.xlarge]
    default_instance_type: c8a.xlarge
    market: on-demand
    max_vcpus: 4
    profiles:
      train:
        instance_types: [g6e.xlarge, g6e.2xlarge, g6e.4xlarge]
        default_instance_type: g6e.xlarge
        market: on-demand
        max_vcpus: 4
        image: deep-learning-gpu
        root_volume_gb: 200
        whole_cores: false
        gpu: true
        rust: false
        datasets:
          artifacts:
            read: [datasets/, models/, runs/, tmp/]
            write: [tmp/]
            write_once: [runs/, models/]
```

The matching central entry would be:

```yaml
projects:
  math-prover:
    instance_types: [c8a.xlarge, g6e.xlarge, g6e.2xlarge, g6e.4xlarge]
    task_profiles: [cpu, train]
    datasets:
      jwall-engineering-math-prover-artifacts:
        prefix: data/
        versioning: true
        write_once: [runs/, models/]
        expire_prefixes: { tmp/: 30 }
        expire_noncurrent_days: 90
```

`rust: false` (on the task-runner and on each profile; default `true`) skips
rustup and the Rust version in the instance setup. The environment block then
shows `rust_toolchain=none`. The pinned `deep-learning-gpu` image has the NVIDIA
driver and CUDA but no PyTorch: the task installs its own pinned PyTorch into
`SCRATCH_DIR` from its lock file. Egress is open, so public Hugging Face
downloads work. The instance receives no secret, so gated models are out of
scope.

## A training workflow

```yaml
name: Train

on:
  workflow_dispatch:
    inputs:
      run_id: { description: Run name, required: true }

permissions: {}

jobs:
  train:
    uses: jwallace145/jwall-engineering-infra/.github/workflows/remote-task.yml@vX.Y.Z
    with:
      project: math-prover
      profile: train
      script: ci/train.sh
      args: '["--run", "${{ inputs.run_id }}"]'
      artifact: train-${{ inputs.run_id }}
    permissions:
      contents: read
      id-token: write
```

`ci/train.sh` runs on the instance as root, at the commit under test. See
[Writing a task script](remote-tasks.md#writing-a-task-script). Besides
`OUT_DIR` and `SCRATCH_DIR`, it gets `TASK_RUN_ID`, `TASK_ID` and
`TASK_COMMIT`, which `task-artifact publish` records in a manifest, and
`TASK_REGION`, which `task-artifact` passes to every AWS CLI call as `--region`.
`AWS_DEFAULT_REGION` is not set.

## Artifacts and `task-artifact`

Long-lived results go to the dataset bucket, never to `OUT_DIR`. A layout that
fits the sample grant:

```text
data/datasets/...                 read-only inputs
data/runs/<run>/checkpoints/...   write-once: one new key per checkpoint
data/runs/<run>/_manifest.json    written last; its presence means "complete"
data/models/<name>/...            write-once
data/tmp/...                      scratch that expires after 30 days
```

Every `write_once` prefix allows `PutObject` only if the request cannot
overwrite: S3 must receive `If-None-Match: *`, and a plain upload is
AccessDenied. A task has no delete permission anywhere.

The task script writes a helper, `/usr/local/bin/task-artifact`, before the
task starts. It is Python 3 with the standard library only, delivered in the
task script so changing it needs no module release.

| Command | What it does |
|---|---|
| `task-artifact put FILE s3://B/KEY` | Hashes FILE with SHA-256, uploads it without overwriting, and reads the object back to confirm this upload wrote it. Prints `<sha256>  <uri>`. |
| `task-artifact get s3://B/KEY FILE` | Downloads to a temporary name, verifies the stored SHA-256 (and size), then renames. A mismatch deletes the temporary file and fails. |
| `task-artifact publish s3://B/PREFIX/ --expect PATHS_FILE [--metadata JSON_FILE]` | Verifies each path listed in PATHS_FILE exists under the prefix with a SHA-256 and the right size, then writes `_manifest.json` last. |
| `task-artifact get-run s3://B/PREFIX/ DIR` | Reads `_manifest.json`, then downloads and verifies every file in it. Refuses a prefix with no manifest. |
| `task-artifact list-complete s3://B/PREFIX/` | Prints the runs directly under the prefix that have a manifest. |

Exit codes: 0 success, 1 failure (including a checksum mismatch), 2 bad usage,
4 the target already exists.

Things to know:

- **Do not use raw `aws s3 cp --no-overwrite` for write-once keys.** On an
  existing key it exits 0 and writes nothing, with no message. A script that
  checks only the exit code believes the upload happened. `task-artifact put`
  avoids this by setting a unique `upload-id` in the metadata and reading it
  back; a different value means the key already existed, and `put` exits 4.
- **`put` and `publish` need `read` on the prefix** as well as `write` or
  `write_once`, because reading the object back (`HeadObject`) needs
  `s3:GetObject`. A profile's `write_once` prefix without a covering `read` is
  a configuration error at load, so it cannot fail after uploading. A `write`
  prefix is not checked: without `read`, `put` uploads and then reports that it
  cannot read the object back, which is why the sample also reads `tmp/`.
- **`CopyObject` into a write-once prefix is refused.** Download and `put` the
  file instead.
- **A retry after a read-back failure exits 4.** If the upload succeeded but
  the read-back failed, the key exists, so the retry cannot tell its own
  earlier write from someone else's. Check the object with `get`.
- **Do not change a file during `put`.** The SHA-256 is computed before the
  upload; a file that changes in between fails the checks.
- **AWS CLI 2.33.0 or later**, as `aws` or `aws2`, is required for
  `--no-overwrite`. Without it the helper exits with an error and uploads
  nothing. The AL2023 image pinned in `bot.amis` has 2.38.0. The GPU image's
  version has not been confirmed: the first GPU smoke test must check the
  `aws_cli=` line of the environment block and run a `put`.
- A partial run has no manifest. Its checkpoints and diagnostics stay for
  debugging and resuming.

## Resuming a long training

A task lasts at most six hours (`max_hours`), the limit of a GitHub job. A
longer training is a **chain of workflow runs**, each resuming from the latest
checkpoint in S3:

1. The task script lists `data/runs/<run>/checkpoints/` and downloads the
   newest with `task-artifact get`, if any.
2. It trains, uploading each checkpoint under a new key with `task-artifact
   put`. A write-once prefix cannot replace a key, so the step is in the name.
3. It stops with margin before six hours, or when the run finishes.
4. The owner (or a later workflow) starts the next run, which resumes.
5. When the run is complete, `task-artifact publish` writes the manifest.

The resume logic belongs to the consumer. `OUT_DIR` is for small files only
(results, summaries, logs): it goes to the results bucket, which expires after
`results_days`, and its upload at the end of a task has a 50-second budget.

## Retention is approved in `config.yaml`

The project file declares retention on each `dataset`, and `config.yaml` must
approve exactly the same values under `projects.<name>.datasets.<bucket>`:

| Field | Meaning |
|---|---|
| `prefix` | The dataset's `prefix`, which the other fields are relative to. Must equal the file's. |
| `versioning` | Keep prior versions. Default `false`. |
| `write_once` | Sub-prefixes that tasks may write only through `write_once` grants. |
| `expire_prefixes` | Sub-prefix to days: current objects there expire. |
| `expire_noncurrent_days` | Days before noncurrent versions expire; needs `versioning`. |

A violation is a configuration error at load, so **Bot apply** and `task run`
refuse it. The checks also reject `write` on a `write_once` prefix, a
`write_once` prefix without a covering `read`, a dataset with `write_once`
prefixes that uses `writers: [task-runner]` (which grants `PutObject` on the
whole prefix), an expiring prefix that overlaps a protected one, and expiring
prefixes that overlap each other or give the same lifecycle rule id (`a/b/`
and `a-b/`). A change to retention is therefore a reviewed change to this
tool. These checks do not stop someone with admin credentials, or an apply
from a modified copy of the tool.

**On a versioned bucket, `expire_prefixes` only adds delete markers.** The
data stays, and is billed, until a noncurrent version expires. Without
`expire_noncurrent_days`, nothing is ever removed. Set both, as the sample
does.

Encryption stays SSE-S3.

## Cost controls

- **The ceiling is $50 per task** (`tasks.max_run_cost_usd`, with an optional
  per-project `max_run_cost_usd`). It is not per chain of runs and not a
  monthly budget. `task run` computes the worst case before launching and
  logs it, for example `worst-case cost: $11.17 (g6e.xlarge on-demand
  $1.861/h x 6 h); ceiling $50.00`. Over it, the launch is refused (exit 2).
  `--max-hours` lowers the estimate. Root volume and public IPv4 (cents an
  hour) are excluded.
- **A Pricing API failure refuses the launch.** After three attempts the
  command exits 3 with `outcome = no-price`. This also reaches rusty-chess at
  its next pin bump: before, its spot path launched without a cap when the
  Pricing API failed.
- **The instance-type allow-list is the hard limit on hourly cost.** The
  ceiling and `max_vcpus` are enforced by the CLI. The launch role is assumed
  by non-fork pull requests and `main`, so code there can call AWS directly
  and skip the CLI, bounded only by the allow-list. When a type is added to
  `projects.<name>.instance_types`, review its price in the same pull request.

At $50 and six hours the threshold is $8.33 an hour. On-demand prices from the
Pricing API on 2026-11-02, us-east-1:

| Type | GPU | vCPU | Instance store | $/h | 6 h |
|---|---|--:|--:|--:|--:|
| `g6e.xlarge` | 1 L40S, 45 GiB | 4 | 250 GB | 1.861 | $11.17 |
| `g6e.2xlarge` | 1 L40S, 45 GiB | 8 | 450 GB | 2.242 | $13.45 |
| `g6e.4xlarge` | 1 L40S, 45 GiB | 16 | 600 GB | 3.004 | $18.03 |
| `g6e.12xlarge` | 4 L40S, 179 GiB | 48 | 3800 GB | 10.493 | $62.96 (blocked at 6 h; allowed at 4 h, $41.97) |

Every current rusty-chess type is under the ceiling; the largest,
`c8a.16xlarge`, is about $20.70 over six hours. Spend per workflow or profile:
`cost report --project <name> [--by profile]` ([costs.md](costs.md)).

## Destroy

`destroy` retires a project. It deletes the task launch templates, instance
roles, log group and network, and keeps the dataset buckets and the task
results bucket: before destroying, it removes the buckets from Terraform state
so `prevent_destroy` does not reject the plan, and logs each one. The IAM
grants stay in state and are destroyed with the roles they sit on. It refuses
while task instances run. It also refuses a file that `config.yaml` no longer
allows, so destroy before removing the project's central entry.

Applying the project again afterwards is not automated. The kept buckets
exist in AWS but not in state, so import them first, with admin credentials or
the apply role, from the project's checkout. Each `aws_s3_bucket*` resource of
the dataset module is imported by the bucket's name, for example:

```sh
terraform import 'module.datasets["artifacts"].aws_s3_bucket.this' jwall-engineering-math-prover-artifacts
```

Repeat for the bucket's sibling resources (`aws_s3_bucket_versioning`,
`aws_s3_bucket_lifecycle_configuration`, `aws_s3_bucket_policy`,
`aws_s3_bucket_public_access_block`, `aws_s3_bucket_ownership_controls` and
`aws_s3_bucket_server_side_encryption_configuration`, all `.this`), and for the
task results bucket (`module.task_results[0]`). `destroy` logs each bucket it
released. The resource names come from the pinned library module and the
import itself has not been run. What Terraform does without the import, whether
it fails because the bucket name is taken or something else, has not been
confirmed against AWS.

**Deleting data** is deliberately not a CLI feature. It is a manual procedure
for an administrator with admin credentials: list what will go, delete the
objects (and, on a versioned bucket, every version and delete marker), then
the bucket. No CI role is granted `s3:DeleteObject` or `s3:DeleteObjectVersion`.

## Not yet proven

- **No real GPU run has happened.** The end-to-end flow (acceptance criterion
  3) is covered only by tests with fake binaries. The first onboarding smoke
  test must run on a `g6e.xlarge` and check `nvidia-smi`, the `aws_cli=` line,
  a `task-artifact put` to a write-once prefix, and instance-store scratch.
- **`ListBucket` prefix-condition behaviour on a missing key** was not tested
  live. `task-artifact` decides absence from listings because a head of a
  missing key may answer 403, but a missing key under a restricted `ListBucket`
  condition has not been tried against S3.
- **`moved` blocks** (`module.image` to `module.image[0]`, and the others) are
  proven by the CI **Bot plan** for rusty-chess, which must show no create or
  destroy.
- **The write-once IAM** was proven in a live test on 2026-11-03 with a
  temporary bucket and role, both deleted: new keys succeed, existing keys
  and plain uploads fail. That test used AWS CLI 2.34.59.
- Whether 45 GiB of GPU memory is enough for a given model depends on the
  consumer's method and has not been tested.
