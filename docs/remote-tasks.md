# Remote tasks: one script on a dedicated, ephemeral EC2 instance

A bot project with a `task-runner` component can run long, CPU-bound jobs,
such as an engine SPRT or a match series, on real dedicated cores instead of
a shared GitHub-hosted runner. A GitHub Actions job calls the reusable
`remote-task.yml` workflow. That workflow runs `jwall-engineering-sites task
run`, which:

1. launches one fresh on-demand instance from the project's task launch template;
2. waits for its setup (git, a C toolchain and a pinned Rust toolchain);
3. fetches the commit under test anonymously and runs one script on it through
   SSM Run Command;
4. streams the script's output into the job's log as it runs;
5. copies the files the script wrote to `$OUT_DIR` back into the job;
6. terminates the instance, on every exit path;
7. exits with the script's own exit code.

It is a task, not a runner: the instance never registers with GitHub, holds
no GitHub credential and exists only for the one script.

## Turning it on

Add the component to the bot's `jwall-engineering-sites.yaml`:

```yaml
components:
  # ... the container-image and lichess-bot components ...
  - kind: task-runner
    instance_types: [c8a.xlarge, c8a.2xlarge]  # the launch role's allow-list
    default_instance_type: c8a.xlarge
    max_hours: 6         # 1-24; the instance shuts itself down (and terminates) after this
    max_concurrent: 4    # 1-16; `task run` refuses beyond this many running tasks
    results_days: 7      # 1-30; task outputs expire from S3 after this
    log_retention_days: 14
```

Every field has the default shown. Burstable `t*` types are refused: they
throttle under sustained load, which is noise in a benchmark. Then, in this
repository:

- add the project and its types to `local.bot_task_types` in
  `foundation/bots.tf` (a test checks that they match the project file);
- release, which applies the foundation and creates the
  `<org>-<project>-ci-task` launch role;
- run **Bot apply** for the project, which creates the launch template, its
  instance role, the security group, the log group and the results bucket.

## The caller

```yaml
name: SPRT

on:
  pull_request:
  workflow_dispatch:

permissions: {}

jobs:
  sprt:
    uses: jwallace145/jwall-engineering-infra/.github/workflows/remote-task.yml@vX.Y.Z
    with:
      project: rusty-chess
      script: scripts/sprt.sh
      args: '["--games", "2000", "--tc", "10+0.1"]'  # a JSON array of strings
      instance-type: c8a.2xlarge                     # optional; the project default otherwise
      artifact: sprt-results                         # the uploaded $OUT_DIR
    permissions:
      contents: read
      id-token: write
```

Inputs: `project` and `script` are required. `args` defaults to `[]`, and
`instance-type` to empty, meaning the project default. `max-hours` defaults
to 0, meaning the project's `max_hours`. `task-id` defaults to `task`;
`artifact` to `task-output`; `artifact-path` to `out`; `debug` to false.

In a matrix, give each leg its own `task-id` and `artifact`: the task ID tags
the instance, names its results prefix and scopes the cleanup step.

Pin the tool release and let Dependabot bump it. The bot repository sets no
variables or secrets. The workflow builds the role ARN from this tool's
`config.yaml` and `project`.

## Writing a task script

- It runs with `bash`, as root, from the repository root at the commit
  under test (the pull request's head commit, or the pushed commit).
- `OUT_DIR` is an empty directory. Everything written there is uploaded to
  S3 and copied into the job's artifact, whether the script succeeds or not.
- `HOME=/root`, and `PATH` includes `/root/.cargo/bin`: `cargo` and `rustc`
  are the pinned toolchain (`tasks.rust_toolchain` in this repository's
  `config.yaml`). `git`, `gcc`, `gcc-c++`, `make`, `tar`, `curl` and the AWS CLI
  are installed. Anything else the script installs itself.
- The script's exit code is the job's exit code.
- Output appears in the job log a few seconds after it is printed (Run
  Command uploads it to CloudWatch in batches). Print progress lines.

The output starts with the environment block, so every result records what
it ran on:

```text
== task-runner environment
image_id=ami-0...
rust_toolchain=1.99.1
rustc=rustc 1.99.1 (...)
cpu_model=AMD EPYC 9R45
physical_cores=4
==
```

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
- **What the role can do.** It can launch only:
  - from the project's `task-runner` template;
  - the project's allow-listed instance types;
  - Amazon-owned images;
  - into the project's network.

  It can pass only the task instance role, and terminate or run commands
  only on instances tagged with the project and `Purpose=ci-task`. The bot
  instance is out of its reach. It can read no secret.
- **What the instance can do.** Task code is pull request code, so the
  instance role can only write: its log group, and objects under the
  results prefix. It has no SSM parameter, secret, image or earlier-result
  access, and `AmazonSSMManagedInstanceCore` is deliberately not attached.
- **No GitHub credential reaches the instance.** It fetches the public
  repository anonymously at the commit SHA. The workflow forwards no token,
  and its checkouts do not persist credentials. Private repositories are
  not supported.
- **Inputs.** The script path must be relative, the ref must be a full
  commit SHA, and the arguments travel base64-encoded to the instance and
  reach the script verbatim.

## Lifecycle and leak guards

An instance that outlives its task costs money. Four things end it:

1. **The CLI terminates it on every exit path.** That covers success, task
   failure, AWS errors and timeouts. It also covers SIGINT and SIGTERM, which
   GitHub sends when a job is cancelled or times out. Later signals cannot
   interrupt the termination.
2. **The workflow's cleanup step.** It runs `if: always()` and terminates
   anything tagged with the run and task.
3. **The instance stops itself.** It shuts down `max_hours` after the task
   starts, and a shutdown terminates it.
4. **`task sweep` by hand.** It terminates anything past its lifetime. There
   is no scheduled sweeper.

Concurrency is capped per project: `task run` refuses (exit 3) when
`max_concurrent` task instances already run.

## Limits

- **Six hours.** GitHub-hosted jobs stop at six hours (`timeout-minutes:
  360`), so budget a task to finish in about 5.5. When the job is stopped,
  the CLI still terminates the instance. Longer work would need an
  asynchronous `task start` and `task collect` pair, which will be built when
  a task needs it.
- **Role session.** The launch role allows sessions of up to 7 hours. The
  workflow requests the task's hours plus one, at most 12.
- **On-demand only.** There is never a spot instance, so a task is never
  interrupted. When a zone has no capacity for the type, the CLI tries the
  project's next zone. When all fail, it exits 3 with "no on-demand <type>
  capacity in <zones>; re-run later".

## Exit codes

| Code | Meaning |
|---|---|
| the task's | the script ran; this is its exit code |
| 1 | the tool or AWS failed: setup, fetch, Run Command, or copying outputs after a successful task |
| 2 | configuration: a type outside the allow-list, `--max-hours` above `max_hours`, or malformed inputs |
| 3 | preflight: the concurrency cap, no capacity, or the wrong role |
| 124 | Run Command's execution timeout (`max_hours`) |
| 130 | interrupted (cancelled) |

## Commands

| Command | Role | What it does |
|---|---|---|
| `task run --script P --ref SHA [--instance-type T] [--max-hours N] [--out DIR] [--run-id ID] [--task-id ID] -- ARGS...` | `ci-task` | Runs the task and exits with its code |
| `task list` | `ci-plan` or `ci-task` | Lists the project's running task instances (read-only) |
| `task terminate (--instance ID \| --run-id ID [--task-id ID] \| --all)` | `ci-task` | Terminates matching task instances |
| `task sweep [--dry-run]` | `ci-task` | Terminates task instances past their `MaxLifetimeHours` |

## Names, tags and costs

| What | Name |
|---|---|
| Launch template, instance role, profile, security group | `<org>-<project>-task` |
| Launch role | `<org>-<project>-ci-task` |
| Log group | `/<org>/<project>/tasks` |
| Results | `s3://<org>-<project>-tasks/tasks/<run>/<task>/` |

Every task instance and its volume carry the project file's six tags.

| Tag | Value |
|---|---|
| `Component` | `task-runner` |
| `Purpose` | `ci-task` |
| `Name` | `<org>-<project>-task-<run>-<task>` |
| `Workflow` | the calling workflow's name |
| `RunId` | the run's ID and attempt |
| `TaskId` | the task's ID |
| `MaxLifetimeHours` | the task's hour limit |

Activate `Purpose` as a cost allocation tag so cost tools can separate task
spend from the bot's.

On-demand us-east-1 list prices, rounded:

| Item | Cost |
|---|---|
| `c8a.xlarge` (4 cores, 8 GiB) | about $0.20 an hour |
| `c8a.2xlarge` (8 cores, 16 GiB) | about $0.40 an hour |
| Public IPv4, 40 GB gp3 for the task's duration | under $0.01 an hour |
| Idle | nothing, apart from stored logs and results |

## Runbook

- **A task seems stuck.** Read its output in the job log. Run `task list`
  (plan or task role) to see what runs. Cancelling the job terminates the
  instance. Failing that, run `task terminate --run-id <run>-<attempt>`.
- **A leaked instance.** `task list` shows it. Run
  `task terminate --instance i-...` or `task sweep`. It also ends itself at
  `max_hours`.
- **Capacity errors.** Re-run later, or choose another allowed type with
  `instance-type`. Adding a zone to `bot.availability_zones` adds a subnet
  to try.
- **Setup failed (exit 1, "instance setup failed").** The job log shows the
  end of `/var/log/task-runner-setup.log`. A rustup or dnf download failed;
  re-run.
- **Rotating the toolchain.** Set `tasks.rust_toolchain` in `config.yaml` to
  the new version, release, then run **Bot apply**. The launch template's new
  default version installs it on the next task.
- **Destroying the project.** `destroy` refuses while task instances run. It
  keeps the task results bucket out of Terraform's way; the bucket expires
  its own contents.
