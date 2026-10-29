# Bot projects: the `container-image` and `lichess-bot` kinds

A repository that publishes a chess engine as GitHub releases can run it
permanently as a Lichess bot with one file, `jwall-engineering-sites.yaml`,
plus its existing lichess-bot config and a short release workflow. The tool
builds an image from each release, pushes it to ECR under the release tag,
and rolls the bot to it on one EC2 instance without ever interrupting a game.
The first consumer is [rusty-chess-v2](https://github.com/jwallace145/rusty-chess-v2).

What you get:

- an ECR repository, `jwall-engineering-<project>`, with immutable release tags;
- one image per release: lichess-bot at a pinned commit, the release's own
  Linux binary (checksum-verified, never rebuilt), and your lichess-bot config;
- one `t3.micro` running the image, with no inbound access at all;
- every new release rolled out automatically, between games, with the old
  version restored if the new one does not start;
- container logs in CloudWatch (30 days) and every finished game shipped to S3
  in a fixed, versioned layout;
- commands to build, push, deploy, check status, read logs and set the token.

## The project file

At the repository root:

```yaml
version: 1
project: rusty-chess
repository: jwallace145/rusty-chess-v2

tags:
  Organization: jwall-engineering
  Project: rusty-chess
  Environment: prod
  Repository: jwallace145/rusty-chess-v2
  ManagedBy: terraform
  Owner: jwallace145

components:
  - kind: container-image
    architecture: x86_64
    artifact:
      asset: rusty-chess-{version}-x86_64-unknown-linux-gnu.tar.gz
      checksum: rusty-chess-{version}-x86_64-unknown-linux-gnu.tar.gz.sha256
      binary: rusty-chess
    # dockerfile: docker/Dockerfile

  - kind: lichess-bot
    track: latest-release
    instance_type: t3.micro
    config: lichess/config.yml
    lichess_user: rusty-chess-bot
    secrets:
      LICHESS_TOKEN: /jwall-engineering/rusty-chess/lichess-token
    engine:
      hash_mb: 128
      move_overhead_ms: 500
    records:
      s3: true
    logs:
      retention_days: 30
```

A file is either a site (`domain` and `site`) or a bot project
(`components`); it can't be both. Unknown fields are errors.

| Field | Type, default | Meaning |
|---|---|---|
| `version` | `1` | File format version. |
| `project` | kebab-case, ≤ 30 | The project; every name below is derived from it. |
| `repository` | `owner/repo` | Where the releases are published. Must match `tags.Repository`. |
| `tags` | six required keys | Applied to every AWS resource for cost allocation. `Project`, `Repository`, `Environment: prod` and `ManagedBy: terraform` are cross-checked; extra keys pass through. |
| `container-image.architecture` | `x86_64` \| `arm64`, `x86_64` | Must match the release asset. `arm64` also needs a `t4g` instance type. |
| `container-image.artifact.asset` | file name with `{version}` | The release asset that holds the binary. `{version}` is the release tag. |
| `container-image.artifact.checksum` | file name with `{version}` | Its `sha256sum` file. The build stops if the digest differs. |
| `container-image.artifact.binary` | file name | The engine inside the tarball's top directory. |
| `container-image.dockerfile` | relative path, none | Replace the harness Dockerfile. Rarely needed. |
| `lichess-bot.track` | `latest-release` | Every newly published release is rolled out. |
| `lichess-bot.instance_type` | `t3.micro` | EC2 instance type. Changing it replaces the instance. |
| `lichess-bot.config` | relative `.yml` path | Your lichess-bot config. It must contain the token as `LICHESS_TOKEN_PLACEHOLDER`. |
| `lichess-bot.lichess_user` | Lichess user name, none | Shown by `bot status` and written into every game record. |
| `lichess-bot.secrets` | map | Environment variable to SSM parameter. Must include `LICHESS_TOKEN`. Paths live under `/jwall-engineering/<project>/` and never under `.../bot/`. |
| `lichess-bot.engine.hash_mb` | 1–4096, 128 | UCI `Hash`. |
| `lichess-bot.engine.move_overhead_ms` | 0–5000, 500 | UCI `Move Overhead`, when the engine offers it. lichess-bot's own `move_overhead` stays in your config. |
| `lichess-bot.records.s3` | bool, `true` | Ship finished games to S3. |
| `lichess-bot.logs.retention_days` | CloudWatch value, 30 | Container log retention. |

What the image enforces in your config, whatever it says:

- **The token** comes from `LICHESS_TOKEN`.
- **The engine** is the release binary.
- **`quit_after_all_games_finish: true`.** One SIGINT then lets the game in progress finish, and the rollouts depend on that.
- **One PGN per game** in the directory the instance ships from.
- **Only advertised UCI options are set.** v0.14.0 has no `Move Overhead`, so it is left out with a warning.

Everything else in the config, including matchmaking, stays yours.

## Names the tool derives

| What | Name |
|---|---|
| Image repository | `jwall-engineering-<project>` |
| Instance, its role, profile and security group | `jwall-engineering-<project>-bot` |
| VPC | `jwall-engineering-<project>` (public subnets in us-east-1a and 1b, no NAT) |
| Records bucket | `jwall-engineering-<project>-records` |
| Log group | `/jwall-engineering/<project>/bot` |
| Parameters | `/jwall-engineering/<project>/bot/{desired-version,running-version,status,last-sync}` |
| CI roles | `jwall-engineering-<project>-ci-{plan,apply,release}` |
| State | `s3://jwall-engineering-tfstate/project=<project>/env=prod/<project>-prod.tfstate` |

## How a release reaches the bot

1. You publish release `vX.Y.Z`. Your caller workflow runs this repository's
   `container-release.yml`, which assumes your `ci-release` role. The role
   trusts only your repository's `v*` tags.
2. `bot reconcile --version vX.Y.Z`:
   - waits for the Linux asset and its `.sha256` to be attached. Release
     workflows publish the release first and upload the build afterwards, so
     this usually takes under a minute; it gives up after `bot.asset_wait`
     in `config.yaml`, 30 minutes by default;
   - downloads them and verifies the checksum;
   - builds the image and runs `rusty-chess bench --depth 6` inside it;
   - pushes the image under the tag;
   - writes the desired version and the status `pending:vX.Y.Z`;
   - asks the instance, through SSM Run Command, to reconcile.
3. On the instance, the reconciler:
   - pulls the image and reads the token, before touching anything;
   - ships finished games;
   - sends lichess-bot **one** SIGINT and **waits** for the game in progress
     to end, however long that takes;
   - starts the new version and gives it 90 seconds to log
     `awaiting challenges`.
4. The workflow follows the status until it reads `ok`, or fails on
   `failed:<reason>`. Your release shows the result as a check. If a newer
   release is deployed while this one waits, this run ends green as
   *superseded*, and the newer run ships.

Guarantees:

- **A rollout never resigns or aborts a game.** The running bot is never
  killed; the swap waits for it.
- **A broken release does not take the bot down.** If the new version does not
  start or does not log its ready line, it is removed and the previous version
  is started again. The status says `failed:<new> did not become healthy; <old>
  restored`.
- **Ordinary failures change nothing.** A pull or a secret read that fails
  leaves the old version playing, and the status says why.
- **A newer release wins.** One deployed while a swap is waiting replaces the
  older target. A run of the same release is harmless.
- **The instance converges at boot.** A rebooted or replaced instance starts
  the desired version on its own.

Status values: `pending:<tag>` (deploy requested), `waiting:<tag>` (the old
version is finishing its game), `rolling-back:<reason>`, `ok`,
`failed:<reason>`.

## Game records

When `records.s3` is on, every finished game is shipped every 10 minutes,
around every swap, and at shutdown, to:

```
s3://jwall-engineering-<project>-records/records/pgn/schema=1/engine=<tag>/date=<YYYY-MM-DD>/<game_id>.pgn
s3://jwall-engineering-<project>-records/records/meta/schema=1/engine=<tag>/date=<YYYY-MM-DD>/<game_id>.json
```

- The `.pgn` is the file lichess-bot wrote, byte for byte, with the engine's
  evaluation comments.
- The `.json` sidecar has exactly these keys: `schema`, `game_id`,
  `engine_version`, `image`, `bot`, `instance_id`, `white`, `black`,
  `result`, `utc_date`, `utc_time`, `time_control`, `variant`, `white_elo`,
  `black_elo`, `event`, `site`, `termination`, `headers` (every PGN header),
  `source_file`, `sha256`, `synced_at`. The [container-instance
  README](https://github.com/jwallace145/terraform-aws-jwall-engineering/tree/main/modules/container-instance#game-records)
  gives each one's type.
- The `key=value` segments are Hive-style partitions, so Athena, Spark or
  DuckDB read `meta/` as a table.

How solid the records are:

- **Correct attribution.** Each engine version writes into its own directory,
  so a game is always filed under the version that played it.
- **Finished games only.** Games without a final result are never shipped.
- **Add only.** Nothing is ever deleted. The instance may add records but not
  read or delete them, and the bucket is versioned and survives `destroy`.
- **Fixed schema.** Any change to the layout or the keys becomes `schema=2`.
- **Known gaps.** Games that ended in the minutes before an unclean instance
  loss are lost. A game interrupted by an instance replacement and resumed is
  filed under the new version.

## Secrets

The Lichess token lives in one SSM SecureString parameter, encrypted with
the account's default `aws/ssm` key. It is never in a file, a log, Terraform
state or an image.

```sh
jwall-engineering-sites bot secrets set LICHESS_TOKEN    # prompts, no echo
jwall-engineering-sites bot secrets check LICHESS_TOKEN  # exit 0 if set
```

Run these from your own shell with admin credentials. Only the instance role
can read the value. The release role can't: it writes only `.../bot/*`.

## Getting a shell

There is no SSH and no open port. Use Session Manager:

```sh
aws ssm start-session --target "$(jwall-engineering-sites bot status | awk '/^instance:/ {print $2}')"
sudo journalctl -u 'container-reconcile*' -u container-records-sync --since today
sudo docker ps; sudo docker logs --tail 100 app
```

## Commands

The commands check that they run as the role in the table. In CI the
workflows assume it. From your own shell with admin credentials, either
assume the role, or set `JES_SKIP_ROLE_CHECK=1` for read-only commands such
as `bot status` and `bot logs`. `bot secrets` needs no role.

| Command | Role | What it does |
|---|---|---|
| `validate` | none | Validate the file and the bot stack offline |
| `plan` | `ci-plan` | Plan `stacks/bot`, with the plan comment |
| `apply` | `ci-apply` | Apply `stacks/bot` |
| `destroy --yes` | `ci-apply` | Destroy the stack. It refuses while images remain; the records bucket is released from state and kept, and the secret stays |
| `bot build --version vX.Y.Z` | none | Download, verify, build and smoke-test locally (Docker) |
| `bot push --version vX.Y.Z` | `ci-release` | Push a built image; refuses an existing tag |
| `bot deploy --version vX.Y.Z [--restart]` | `ci-release` | Roll the bot to a pushed tag and follow the swap |
| `bot reconcile [--version vX.Y.Z] [--dry-run]` | `ci-release` | Build, push and deploy as needed; idempotent |
| `bot status` | `ci-plan` or `ci-release` | Instance, versions, status, last records sync |
| `bot logs [--since 10m] [--follow]` | `ci-plan` | The container's CloudWatch logs |
| `bot secrets set\|check NAME` | your shell | Write or check a secret |

## The caller workflow

Commit this in the bot repository as `.github/workflows/release-to-lichess.yml`:

```yaml
name: Release to Lichess

on:
  release:
    types: [published]

permissions: {}

jobs:
  release:
    uses: jwallace145/jwall-engineering-infra/.github/workflows/container-release.yml@vX.Y.Z
    with:
      project: rusty-chess
    permissions:
      contents: read
      id-token: write
```

Pin the tool release that ships the workflow, and let Dependabot bump it.
The bot repository sets no variables or secrets. The workflow builds the
release role's ARN from this tool's `config.yaml` (`aws.account_id`,
`context.organization`) and `project`, and the role's trust policy accepts
only that repository's `v*` tags. A `release-role-arn` input can override the
derived role.

## Costs

On-demand us-east-1 list prices, rounded:

| Item | Monthly |
|---|---|
| `t3.micro` (730 h) | about $7.60 |
| Public IPv4 address | about $3.65 |
| 16 GB gp3 root volume | about $1.30 |
| CloudWatch Logs (ingest and 30 days of storage) | under $1 |
| ECR (20 images of about 150 MB) | about $0.30 |
| S3 records and SSM | cents |
| **Total** | **about $13–14** |

There is no NAT gateway, load balancer or ECS. The CPU runs on standard
credits, so a busy engine slows down rather than running up charges. The
largest saving would be Graviton (`t4g.micro`), which needs an arm64 release
asset.

## Applying and replacing

The bot's stack is planned on every pull request of this repository: the
`Bot plan` job posts a comment. Merging the pull request does not apply it.
It is applied by hand with the **Bot apply** workflow (`workflow_dispatch`,
`prod` environment), and its plan comment says what the next Bot apply would
do.

A change to the instance's image, instance type or user data **replaces the
instance** and ends the game in progress. That includes a library bump that
changes the on-instance scripts. Read the plan comment and run the apply
while the bot is idle. Games already finished are shipped at shutdown.

**The image is pinned by name.** `bot.amis` in `config.yaml` holds one
Amazon Linux 2023 image per architecture, by its Amazon image name, such as
`al2023-ami-2023.12.20261026.1-kernel-6.18-x86_64`. The bot stack resolves
the name to the image ID among Amazon-owned images, including deprecated
ones, so a pin keeps working after Amazon deprecates it. A new Amazon release
never shows up as a replacement by itself. To upgrade the OS:

1. Look up the name of the current latest image:
   ```sh
   aws ec2 describe-images --image-ids "$(aws ssm get-parameter \
     --name /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64 \
     --query Parameter.Value --output text)" --query 'Images[0].Name' --output text
   ```
2. Change `bot.amis.x86_64` to that name in a pull request. Its bot plan
   shows the replacement.
3. After merging, run Bot apply while the bot is idle.

An architecture with no pin follows the latest image, and is replaced on
every Amazon release.

## Runbook

**The bot is not playing.**

1. Run `bot status`. If there is no instance or it is stopped, run Bot apply.
2. If the status is `failed:...`, read `bot logs --since 1h`. Then fix and
   cut a new release, or roll back as below.
3. If the status is `ok` but there are no games, check `bot logs`. Common
   causes are a revoked token and challenge filters in your config.

**A swap looks stuck** at `waiting:<tag>`. That is normal: the old version is
finishing its game, and slow time controls can take 20 minutes or more. If it
lasts longer than any game could, open a shell and check `docker ps` and
`journalctl -u 'container-reconcile*'`. Do not kill the container unless you
accept losing that game.

**Roll back by hand.** Run `bot deploy --version <older tag>` with the
release role, or re-run the older release's workflow. Note that the next
published release rolls forward again.

**A release fails to build.** The release's check shows why, and nothing was
pushed or deployed. The causes are:

- an asset still missing after the wait (`release vX.Y.Z still has no ...`),
  which usually means the release's own build failed;
- a checksum mismatch;
- a failed bench.

Fix it and publish a new release. If the assets were only late, re-run the
failed run once they are attached.

**Rotate the token.**

1. Create a new token on Lichess.
2. Run `bot secrets set LICHESS_TOKEN`.
3. Run `bot deploy --version <running tag> --restart`. It waits for the game
   in progress and restarts with the new token.
4. Revoke the old token.

**Destroy.**

1. Delete the images with admin credentials. The image repository refuses
   deletion while it holds any, and `destroy` stops with the exact command
   to run.
2. Run Bot apply with `destroy`, or `destroy --yes` with the apply role.

The records bucket is released from Terraform state first, because its
`prevent_destroy` would otherwise reject the whole plan. The bucket and its
games are kept, along with the secret parameter.

## Owner setup, once

No repository variables or secrets are needed: every role ARN is derived
from `aws.account_id` and `context.organization` in this tool's
`config.yaml` and the project name.

1. **Register the bot.**
   - Add it to `foundation/bots.tf` (repository and `repository_id`), to the
     `Bot plan` matrix in `ci.yml`, and to the `Bot apply` choices. A test
     keeps the three in sync.
   - Merge, then release this repository. The foundation apply creates the
     three CI roles; until then, bot plans are skipped with a notice.
2. **Prepare the bot repository.** On its default branch, commit the project
   file, the caller workflow and any config changes
   (`matchmaking.allow_matchmaking: true` if wanted). Bot apply reads the
   project file from there.
3. **Apply the stack.** Run **Bot apply** for the project.
4. **Set the token.** Run `bot secrets set LICHESS_TOKEN` from your shell
   with admin credentials.
5. **Stop the laptop bot.** Exactly one process may hold the token.
6. **Ship the newest release.** Publish a new release, which runs the caller
   workflow. Or, for a release published before the workflow existed, run
   `bot reconcile --version <tag>` from your shell with admin credentials
   and `JES_SKIP_ROLE_CHECK=1`; this needs Docker.
