# Remote tasks: one script on a dedicated, ephemeral EC2 instance

A bot project with a `task-runner` component can run long, CPU-bound jobs,
such as an engine SPRT or a match series, on real dedicated cores instead of
a shared GitHub-hosted runner. The standard box is a 64-core `c8a.16xlarge`
on spot. A GitHub Actions job calls the reusable `remote-task.yml` workflow,
which runs `jwall-engineering-sites task run`. That command:

1. waits for room under the project's vCPU cap;
2. launches one fresh spot instance from the project's task launch template,
   trying every zone, and falls back to on demand only at launch;
3. waits for its setup (git, a C toolchain and a pinned Rust toolchain);
4. fetches the commit under test anonymously and runs one script on it through
   SSM Run Command, with a watcher for spot interruption notices;
5. streams the script's output into the job's log as it runs;
6. copies the files the script wrote to `$OUT_DIR` back into the job;
7. terminates the instance, on every exit path;
8. exits with the script's own exit code, or 75 when spot reclaimed the
   instance, and reports the outcome to the workflow.

It is a task, not a runner: the instance never registers with GitHub, holds
no GitHub credential and exists only for the one script.

## Turning it on

Add the component to the bot's `jwall-engineering-sites.yaml`:

```yaml
components:
  # ... the container-image and lichess-bot components ...
  - kind: task-runner
    instance_types: [c8a.xlarge, c8a.8xlarge, c8a.16xlarge]  # the launch role's allow-list
    default_instance_type: c8a.16xlarge  # 64 physical Zen 5 cores, the bot's core
    market: spot                 # spot | on-demand
    max_price_fraction: 0.58     # spot cap as a fraction of the on-demand price
    on_demand_fallback: true     # on demand at launch only, after spot fails everywhere
    spot_wait_minutes: 5         # 1-30; how long spot is retried across the zones
    max_hours: 6                 # 1-6; the instance shuts itself down (and terminates) after this
    max_vcpus: 192               # 1-256; running task vCPUs per project
    slot_wait_minutes: 30        # 0-120; how long a launch over the cap waits for a slot
    results_days: 7              # 1-30; task outputs expire from S3 after this
    log_retention_days: 14
```

Every field has the default shown. Burstable `t*` types are refused, because
they throttle under sustained load, which is noise in a benchmark. Types with
more than one thread per core are refused at launch (exit 2): a timed game
needs a whole core. Then, in this repository:

- add the project and its types to `local.bot_task_types` in
  `foundation/bots.tf` (a test checks that they match the project file);
- release, which applies the foundation and creates the
  `<org>-<project>-ci-task` launch role;
- run **Bot apply** for the project. It creates the launch template, its
  instance role, the security group, the log group and the results bucket,
  plus a public subnet in each zone of `bot.availability_zones`.

## The caller

```yaml
name: SPRT

on:
  pull_request:
  workflow_dispatch:

permissions: {}

jobs:
  play:
    strategy:
      matrix: { mode: [timed, nodes] }
    uses: jwallace145/jwall-engineering-infra/.github/workflows/remote-task.yml@vX.Y.Z
    with:
      project: rusty-chess
      script: ci/strength-task.sh
      args: '["--mode", "${{ matrix.mode }}"]'  # a JSON array of strings
      artifact: sprt-${{ matrix.mode }}        # the uploaded $OUT_DIR; also names the task
    permissions:
      contents: read
      id-token: write

  report:
    needs: play
    if: always()
    runs-on: ubuntu-latest
    steps:
      - run: echo "outcome ${{ needs.play.outputs.outcome }}"
```

**Inputs.** `project` and `script` are required. The rest are optional:

| Input | Default |
|---|---|
| `args` | `[]` |
| `instance-type` | empty: the project's default type |
| `market` | empty: the project's `market` |
| `max-hours` | 0: the project's `max_hours` |
| `task-id` | the `artifact` name |
| `artifact` | `task-output` |
| `artifact-path` | `out` |
| `debug` | false |

**Outputs.** The workflow sets two outputs:

| Output | Meaning |
|---|---|
| `outcome` | `passed`, `failed`, `interrupted` (spot reclaimed the instance) or `no-capacity` |
| `interrupted-at` | when the instance was reclaimed; set only for `interrupted` |

An interrupted task's partial outputs upload as `<artifact>-interrupted`, and
the step fails, so a gate can never pass on an interrupted run.

**Matrices.** Give each leg its own `artifact`; the upload refuses duplicate
names anyway. The task ID defaults to the artifact name. It tags the instance
and scopes the cleanup step, and `task run` refuses (exit 3) to start a task
whose run and task ID are already running.

Pin the tool release and let Dependabot bump it. The bot repository sets no
variables or secrets. The workflow builds the role ARN from this tool's
`config.yaml` and `project`.

## Writing a task script

- **Where it runs.** With `bash`, as root, in its own process group, from
  the repository root at the commit under test: the pull request's head
  commit, or the pushed commit.
- **Outputs.** `OUT_DIR` is an empty directory. Everything written there is
  uploaded to S3 and copied into the job's artifact, whether the script
  succeeds, fails or is interrupted.
- **Toolchain.** `HOME=/root`, and `PATH` includes `/root/.cargo/bin`, so
  `cargo` and `rustc` are the pinned toolchain (`tasks.rust_toolchain` in
  this repository's `config.yaml`). `git`, `gcc`, `gcc-c++`, `make`, `tar`,
  `curl` and the AWS CLI are installed. The script installs anything else
  itself.
- **Exit code.** The script's exit code is the job's. Avoid 75: on spot, a
  non-zero exit counts as an interruption when the watcher's record or AWS's
  termination reason says spot reclaimed the instance, and 75 is that code.
- **Concurrency.** Run at most physical cores minus two games at once, or
  timed games steal each other's clock. That is 62 on a `c8a.16xlarge`; the
  brief settles on 60.
- **Output timing.** Output appears in the job log a few seconds after it
  is printed, because Run Command uploads it to CloudWatch in batches.
  Print progress lines. The CLI reads until the script's end marker
  arrives, so the last lines are never cut off.
- **Interruptions.** On a spot interruption notice the watcher prints
  `SPOT INTERRUPTION: <time>`, writes `interrupted.json` to `OUT_DIR` and
  uploads it at once. It then sends SIGTERM to the script's process group,
  and SIGKILL 45 seconds later. The outputs so far are uploaded, for at
  most 50 seconds, and AWS terminates the instance two minutes after the
  notice. Trap SIGTERM only to save partial results quickly. Partial logs
  are for reading, never for a verdict.

The output starts with the environment block, so every result records what
it ran on:

```text
== task-runner environment
image_id=ami-0...
rust_toolchain=1.99.1
rustc=rustc 1.99.1 (...)
cpu_model=AMD EPYC 9R45
physical_cores=64
==
```

## Spot, prices and capacity

- **The cap.** Spot has no bidding: you pay the current spot price. The
  maximum only decides when AWS stops the instance *for price*. The cap is
  `max_price_fraction` × the on-demand price from the Pricing API, rounded
  down. For a `c8a.16xlarge` that is 0.58 × $3.4486 = $2.0002 an hour, which
  guarantees a test never costs more than 58% of on demand. The log shows
  the cap next to the current price. If the Pricing API is unavailable,
  spot is requested without a cap, so AWS caps it at the on-demand price,
  and the log says so.
- **Capacity interruptions** are the common kind. AWS reclaims spot capacity
  whatever you pay. The CLI reduces them in two ways:
  - It tries the zones in order of their spot placement score, then the
    cheapest current price. Both are logged.
  - It keeps tasks short: one task is one SPRT leg.
- **No capacity at launch.** The CLI retries spot across every zone for
  `spot_wait_minutes`. Then, when `on_demand_fallback` is on, it launches on
  demand and says so plainly; otherwise it exits 3 with `outcome =
  no-capacity`. It never falls back mid-task: an interrupted task is
  reported, not resumed.
- **Zones.** `bot.availability_zones` lists every us-east-1 zone that
  offers c8a (1a, 1b, 1c, 1d, 1f; 1e does not). Append only: subnets are
  numbered by position, and the bot lives in the first.

## Security model

- **Who can launch.** The `<org>-<project>-ci-task` role trusts only the bot
  repository's `pull_request` events and its `main` branch, by exact OIDC
  subject.
  - GitHub issues no OIDC token to pull requests from forks.
  - The workflow's first step refuses fork pull requests ("fork pull
    requests cannot use the standardized task runners"). It also refuses
    every event other than `pull_request`, `push`, `workflow_dispatch` and
    `schedule`. This matters because `pull_request_target`,
    `issue_comment` and `workflow_run` runs on `main` carry the same subject
    as a push to `main` and can carry fork code.
  - Protect `.github/workflows` in the bot repository with CODEOWNERS.
- **What the role can do.** It can launch, on spot or on demand, only:
  - from the project's `task-runner` template;
  - the project's allow-listed instance types;
  - Amazon-owned images;
  - into the project's network.

  It can pass only the task instance role, and terminate or run commands
  only on instances tagged with the project and `Purpose=ci-task`. The bot
  instance is out of its reach. It can read prices and placement scores,
  and no secret.
- **What the instance can do.** Task code is pull request code, so the
  instance role can only write.
  - **Results:** objects under its own prefix, `tasks/<role-id>:<instance-id>/`,
    so no task can touch another's results.
  - **Logs:** the task log group, which the project's tasks share. Rest a
    verdict on the results, not on the streamed log.

  It has no SSM parameter, secret, image or earlier-result access.
  `AmazonSSMManagedInstanceCore` is deliberately not attached.
- **No GitHub credential reaches the instance.** It fetches the public
  repository anonymously at the commit SHA. The workflow forwards no token,
  and its checkouts do not persist credentials. Private repositories are
  not supported.
- **Inputs.** The script path must be relative and the ref a full commit SHA.
  The arguments travel base64-encoded to the instance and reach the script
  verbatim.

## Lifecycle and leak guards

An instance that outlives its task costs money. Four things end it:

1. **The CLI terminates it on every exit path.** That covers success, task
   failure, interruption, AWS errors and timeouts. It also covers SIGINT and
   SIGTERM, which GitHub sends to the step's process when a job is cancelled
   or times out. The workflow `exec`s the CLI so that process is the CLI
   itself, and later signals cannot interrupt the termination. A
   not-yet-visible instance right after launch is retried for a minute
   before it counts as gone.
2. **The workflow's cleanup step.** It runs `if: always()` and terminates
   anything tagged with the run and task.
3. **The instance stops itself.** It shuts down a minute after its task
   ends, and at the latest `max_hours` after the task starts. A shutdown
   terminates it.
4. **`task sweep` by hand.** It terminates anything older than its lifetime
   plus an hour of setup headroom. There is no scheduled sweeper.

Concurrency is capped per project in vCPUs. A launch that would exceed
`max_vcpus` waits up to `slot_wait_minutes` for running tasks to finish, then
exits 3 naming them. The default of 192 vCPUs fits three `c8a.16xlarge`
(one pull request's two legs plus a match) inside the account's 256-vCPU spot
and on-demand quotas, and smaller types fit more. The check happens before
the launch, so legs that start at the same moment can exceed it until those
tasks end.

## Limits

- **Six hours.** GitHub-hosted jobs stop at six hours (`timeout-minutes:
  360`), so budget a task to finish in about 5.5. When the job is stopped,
  the CLI still terminates the instance. Longer work would need an
  asynchronous `task start` and `task collect` pair, which will be built when
  a task needs it.
- **Role session.** The launch role allows sessions of up to 7 hours. The
  workflow requests the task's hours plus one, so `max_hours` is at most 6.

## Exit codes

| Code | Meaning |
|---|---|
| the task's | the script ran; this is its exit code |
| 1 | the tool or AWS failed: setup, fetch, Run Command, or copying outputs after a successful task |
| 2 | configuration: a type outside the allow-list or with SMT, a bad market or price fraction, `--max-hours` above `max_hours`, or malformed inputs |
| 3 | preflight: no free slot, no capacity, a duplicate task, or the wrong role |
| 75 | spot reclaimed the instance: there is no result; re-run |
| 124 | Run Command's execution timeout (`max_hours`) |
| 130 | cancelled |

## Commands

| Command | Role | What it does |
|---|---|---|
| `task run --script P --ref SHA [--instance-type T] [--market spot\|on-demand] [--max-price-fraction F] [--on-demand-fallback\|--no-on-demand-fallback] [--max-hours N] [--out DIR] [--run-id ID] [--task-id ID] [--pull-request N] -- ARGS...` | `ci-task` | Runs the task, writes `outcome` and `interrupted-at` to `GITHUB_OUTPUT`, and exits with the task's code |
| `task list` | `ci-plan` or `ci-task` | Lists the project's running task instances (read-only) |
| `task terminate (--instance ID \| --run-id ID [--task-id ID] \| --all)` | `ci-task` | Terminates matching task instances |
| `task sweep [--dry-run]` | `ci-task` | Terminates task instances past their `MaxLifetimeHours` |

## Names, tags and costs

| What | Name |
|---|---|
| Launch template, instance role, profile, security group | `<org>-<project>-task` |
| Launch role | `<org>-<project>-ci-task` |
| Log group | `/<org>/<project>/tasks` |
| Results | `s3://<org>-<project>-tasks/tasks/<role-id>:<instance-id>/` |

Every task instance and its volume carry the project file's six tags.

| Tag | Value |
|---|---|
| `Component` | `task-runner` |
| `Purpose` | `ci-task` |
| `Lifecycle` | `spot` or `on-demand` |
| `Name` | `<org>-<project>-task-<run>-<task>` |
| `Workflow` | the calling workflow's name |
| `RunId` | the run's ID and attempt |
| `TaskId` | the task's ID |
| `PullRequest` | the pull request number, when there is one |
| `MaxLifetimeHours` | the task's hour limit |

Activate `Purpose` and `Lifecycle` as cost allocation tags so cost tools can
separate task spend from the bot's, and spot from on demand.

us-east-1 prices on 2026-10-29, rounded. Billing is per second.

| Item | On demand | Spot (by zone) |
|---|---|---|
| `c8a.16xlarge` (64 cores, 128 GiB) | $3.45 an hour | $0.93 to $1.39 an hour |
| `c8a.8xlarge` (32 cores, 64 GiB) | about $1.72 an hour | about half |
| `c8a.xlarge` (4 cores, 8 GiB) | about $0.22 an hour | less |
| Public IPv4, 40 GB gp3 | under $0.01 an hour | the same |
| Idle | nothing, apart from stored logs and results | nothing |

A 20-minute SPRT leg on a spot `c8a.16xlarge` at about $1 an hour costs
about $0.35.

## Runbook

- **A task seems stuck.** Read its output in the job log. Run `task list`
  (plan or task role) to see what runs. Cancelling the job terminates the
  instance. Failing that, run `task terminate --run-id <run>-<attempt>`.
- **A leaked instance.** `task list` shows it. Run
  `task terminate --instance i-...` or `task sweep`. It also ends itself at
  `max_hours`.
- **An interrupted task (exit 75, `outcome = interrupted`).** There is no
  result. Re-run the job ("Re-run failed jobs", or push again). The
  `-interrupted` artifact holds the partial log.
- **A run of interruptions or no-capacity launches.** Read the placement
  scores and prices in the launch log lines. Re-check placement with
  `aws ec2 get-spot-placement-scores`. For an urgent run, set `market:
  on-demand` on the job (about $3.45 an hour for a `c8a.16xlarge`), or use a
  smaller type.
- **Waiting for a slot.** The log names the running tasks. Wait, or stop
  stale ones with `task terminate`.
- **Setup failed (exit 1, "instance setup failed").** The job log shows the
  end of `/var/log/task-runner-setup.log`. A rustup or dnf download failed;
  re-run.
- **Rotating the toolchain.** Set `tasks.rust_toolchain` in `config.yaml` to
  the new version, release, then run **Bot apply**. The launch template's new
  default version installs it on the next task.
- **Destroying the project.** `destroy` refuses while task instances run. It
  keeps the task results bucket out of Terraform's way; the bucket expires
  its own contents.
