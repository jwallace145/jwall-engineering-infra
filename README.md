# jwall-engineering-infra

The foundation stack for every [jwall.engineering](https://jwall.engineering) site. It owns:

- the shared Terraform state bucket `jwall-engineering-tfstate`
- a reference to the account's GitHub Actions OIDC identity provider (owned by prog-strength-infra for now)
- the `jwall.engineering` Route 53 hosted zone (zone-level records only)
- for each site in the `projects` registry, an S3 bucket and three CI roles
- its own plan and apply CI roles
- the `jwall-engineering-sites` CLI that every site repository's CI runs to plan, preview, apply and publish, its image in a private ECR repository, and the reusable workflows that run it
- bot projects: for each entry in the `bots` registry, three CI roles, and the bot stack (`stacks/bot`) that runs a repository's GitHub releases as a permanent Lichess bot ([guide](docs/lichess-bot.md))

Everything is in `us-east-1`, managed with Terraform `>= 1.11` and the AWS provider `~> 6.0`. The shared building blocks are consumed from the public Terraform Registry, see [Shared modules](#shared-modules-from-the-terraform-registry). Releases are cut and applied by [Release Please](#releases-and-applies). Contribution rules are in [CONTRIBUTING.md](CONTRIBUTING.md); the rules for coding agents are in [AGENTS.md](AGENTS.md).

## jwall-engineering-sites: the sites CLI

`jwall-engineering-sites` is the Python CLI (Typer, managed with `uv`) that
provisions and publishes every site from one YAML file in its repository,
`jwall-engineering-sites.yaml`. It wraps Terraform and S3: it renders the
bundled site stack in `stacks/site/` from that file and `config.yaml`, runs
`terraform` against the shared state bucket, syncs built content and
invalidates CloudFront. Site repositories keep no Terraform. It ships as a
versioned image in the private ECR repository `jwall-engineering-sites`,
published by the release workflow, and site repositories run it through the
reusable workflows this repository publishes. The design is in
[#26](https://github.com/jwallace145/jwall-engineering-infra/pull/26).

| Command | Role it expects | What it does |
| --- | --- | --- |
| `validate` | none | Validate both files, render the stack, `terraform validate` offline |
| `plan` | `ci-plan` | Plan with the derived backend; writes the plan comment and `changes=true\|false` |
| `preview deploy --pr N --dist DIR`, `preview remove --pr N` | `ci-preview` | Publish or remove `previews/pr-N/` |
| `apply` | `ci-apply` | Init, plan, apply, write `outputs.json` |
| `publish --dist DIR [--environment prod\|dev]` | `ci-apply` | Sync content and invalidate the distribution |
| `release --dist DIR` | `ci-apply` | `apply` then `publish` |
| `outputs`, `config show` | `ci-plan` / none | Stack outputs as JSON; the effective tool configuration |
| `report plan-comment --exit-code N --plan FILE --subject S` | none | Render the branded plan comment from a saved plan log (this repo's own CI uses it) |
| `destroy --yes` | `ci-apply` | Bot projects only: destroy the stack (the records bucket and the secret are kept) |
| `bot build --version vX.Y.Z` | none | Bot projects: download and verify the release asset, build and smoke-test the image (Docker) |
| `bot push --version vX.Y.Z` | `ci-release` | Push the image under the release tag; refuses an existing tag |
| `bot deploy --version vX.Y.Z [--restart]` | `ci-release` | Roll the bot to a pushed tag and follow the swap (waits for the game in progress) |
| `bot reconcile [--version vX.Y.Z] [--dry-run]` | `ci-release` | Build, push and deploy as needed; idempotent; what the release workflow runs |
| `bot status` | `ci-plan` or `ci-release` | Instance, versions and rollout status |
| `bot logs [--since D] [--follow]` | `ci-plan` | The container's CloudWatch logs |
| `bot secrets set\|check NAME` | the owner's shell | Write (no echo) or check a SecureString secret |

Every command checks that the credentials are the role it expects, that the
built site and bucket exist, and that the Terraform in the image is the
version `config.yaml` pins, before touching anything. Failures exit 1
(Terraform or AWS), 2 (configuration) or 3 (preflight). Run artifacts and a
debug log go to `./.jwall-engineering-sites/`.

```sh
uv sync                                  # the CLI and its dev tools
uv run jwall-engineering-sites --help
uv run ruff check && uv run mypy && uv run pytest
docker build --build-arg "TERRAFORM_VERSION=$(yq -r .terraform.version config.yaml)" -t jwall-engineering-sites:local .
```

### Reusable workflows for site repositories

Site repositories do not run the image themselves. They call the reusable
workflows this repository publishes, pinned to an exact tag that Dependabot
bumps. The workflow at tag `vX.Y.Z` runs image `vX.Y.Z` (the composite action
`.github/actions/jwall-engineering-sites` pins the tag, and Release Please
bumps it with the package version), so one pin covers both.

| Workflow | Calls | Caller needs |
| --- | --- | --- |
| `site-plan.yml` | `validate`, then `plan`, and posts the sticky plan comment | `plan-role-arn`; `id-token: write`, `pull-requests: write`; optionally `app-client-id` and the `app-private-key` secret |
| `site-preview.yml` | `preview deploy` on open/synchronize/reopen, `preview remove` on close, and the sticky preview comment | `artifact`, `preview-role-arn`; the same permissions and optional app inputs |
| `site-release-please.yml` | Release Please with the release bot; outputs `release_created` and `tag` | `secrets: inherit` |
| `site-deploy.yml` | `release` in the caller's `prod` environment at the tag | `artifact`, `tag`, `apply-role-arn`; `id-token: write` |

Every job uploads `.jwall-engineering-sites/` (the debug log, plan text and
comment files) as an artifact. A site's preview workflow is a build job plus
one call:

```yaml
on:
  pull_request:
    types: [opened, synchronize, reopened, closed]
jobs:
  build:
    if: github.event.action != 'closed'
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
      - uses: astral-sh/setup-uv@v7
      - run: uv sync --frozen && uv run zensical build --strict --clean
      - uses: actions/upload-artifact@v4
        with: { name: site, path: site }
  preview:
    needs: build
    if: always() && !cancelled()
    uses: jwallace145/jwall-engineering-infra/.github/workflows/site-preview.yml@v0.6.1
    with:
      artifact: site
      preview-role-arn: ${{ vars.AWS_PREVIEW_ROLE_ARN }}
      app-client-id: ${{ vars.JES_APP_CLIENT_ID }} # optional: comments come from the app
    secrets:
      app-private-key: ${{ secrets.JES_APP_PRIVATE_KEY }}
    permissions: { contents: read, id-token: write, pull-requests: write }
```

### Bot projects

A repository whose `jwall-engineering-sites.yaml` has `components` (one
`container-image` and one `lichess-bot`) instead of `domain` and `site` is a
bot project. `validate`, `plan`, `apply` and `outputs` work on it with the
bot stack, `stacks/bot`. That stack holds the image repository, a VPC with
public subnets and no NAT, the records bucket and the bot instance, all from
the library at the same pin. The `bot` commands build, push and roll out
images.

The bot's stack is planned on every pull request here (the `Bot plan` job)
and applied by hand (the `Bot apply` workflow). An apply can replace the
instance and end the game in progress, so it is never automatic. The bot
repository's own release workflow calls `container-release.yml`, which runs
`bot reconcile` with the bot's release role. The full guide, with every
field, the rollout guarantees, the game-records layout, costs and a
runbook, is [docs/lichess-bot.md](docs/lichess-bot.md). A note for the bot's
own README is in [docs/consumer-note.md](docs/consumer-note.md).

### The pull request comments

The plan and preview comments are the product's face in a site repository, so
the CLI renders them (`report.py`) as one branded card rather than raw plan
text: a centred **JWall Engineering Sites** header naming the site, one
GitHub alert that carries the verdict, the detail a reviewer needs without
expanding anything, the full Terraform output folded away, and a footer with
the tool version, the commit and the workflow run. The alert is conditional
on what the plan found:

| Plan result | Alert | Shown without expanding |
| --- | --- | --- |
| No changes | `NOTE`: merging will not touch AWS | nothing else |
| Changes | `WARNING` with the add/change/destroy counts | a counts table and a per-resource action table |
| Any destroy or replace | `CAUTION` naming how many resources are destroyed or replaced | the same tables, with 🗑️ and ♻️ rows to check |
| Plan failed | `CAUTION` | the Terraform output, already expanded |

The resource table lists at most 40 resources (the rest are in the full
output) and the whole comment is kept under `plan.comment_max_bytes` from
`config.yaml`, truncating the plan text from the head. This repository's own
`Terraform plan` job renders its comment with `report plan-comment`, so the
foundation plan and the site plans look the same.

**Who posts the comment.** By default the sticky comment is authored by
`github-actions[bot]`. Pass `app-client-id` (and the `app-private-key`
secret) to `site-plan.yml` and `site-preview.yml`, or set `JES_APP_CLIENT_ID`
and `JES_APP_PRIVATE_KEY` in this repository, and the workflows mint a
short-lived installation token for the **JWall Engineering Sites** GitHub
App and post as it, with the app's name and avatar. The app needs only
*Pull requests: read and write* and has to be installed on every repository
that posts. If the token cannot be minted the step warns and the comment
falls back to `github-actions[bot]`. A comment that already exists on a pull
request keeps its original author; the app shows up on new pull requests.

The release workflow (`workflow_dispatch` with a `tag`) can re-publish the
image for an existing tag without cutting a release.

## Shared modules from the Terraform Registry

This stack is a consumer of [`jwallace145/jwall-engineering/aws`](https://registry.terraform.io/modules/jwallace145/jwall-engineering/aws), the jwall-engineering module library published to the public Terraform Registry from [`jwallace145/terraform-aws-jwall-engineering`](https://github.com/jwallace145/terraform-aws-jwall-engineering). The library is open source under MIT, versioned with semver, and released with the same Release Please flow as this repo. Consuming it here, the same way any third party would, is deliberate: the foundation stack eats its own dog food, so the modules are exercised in production before anyone else depends on them.

| Submodule | Used for | Where |
| --- | --- | --- |
| `//modules/context` | The tag set applied to every resource through `default_tags` | `context.tf` |
| `//modules/tfstate-bucket` | The shared Terraform state bucket | `state.tf` |
| `//modules/hosted-zone` | The `jwall.engineering` zone, its CAA record and the GitHub Pages domain-verification TXT record | `dns.tf` |
| `//modules/ci-plan-role` | Plan roles for this repo and each project, with read policies from the `ci-policies` catalog | `ci-roles.tf`, `projects.tf` |
| `//modules/ci-preview-role` | Preview role per project, with `static-site/previews` | `projects.tf` |
| `//modules/ci-apply-role` | Apply roles for this repo and each project, with read and write policies from the `ci-policies` catalog | `ci-roles.tf`, `projects.tf` |
| `//modules/site-bucket` | One S3 bucket per project, `jwall-engineering-<project>` | `projects.tf` |
| `//modules/ecr-repository` | The private repository for the `jwall-engineering-sites` CLI image, configured from `config.yaml` | `ecr.tf` |

The root module itself creates no resources directly. It is the composition: identity, the `projects` map, module calls, and outputs.

Module sources look like this, with an exact `version` on every block:

```hcl
module "site_bucket" {
  for_each = local.projects
  source   = "jwallace145/jwall-engineering/aws//modules/site-bucket"
  version  = "0.10.1"

  project = each.key
}
```

Versions are pinned exactly, not with `~>`, because Terraform records provider versions in `.terraform.lock.hcl` but not module versions; an exact pin is the only way two applies of the same commit resolve the same code. Every pin (in `foundation/` and both stacks under `stacks/`) is always bumped together, and so is `library.version` in `config.yaml`.

**Upgrading.** When the library publishes a release, Dependabot's Terraform ecosystem opens a `chore(deps)` PR bumping the pins. The CI plan comment on that PR shows exactly what the new version changes in AWS. Read the library's [CHANGELOG](https://github.com/jwallace145/terraform-aws-jwall-engineering/blob/main/CHANGELOG.md) entry, check the plan, merge, and the change applies with the next release of this repo. While the library is on `0.x`, a breaking change bumps the minor version and is marked `!` in its changelog.

## Layout

The foundation Terraform stack lives in `foundation/`; every `.tf` path below
is relative to it. The repository root holds the `jwall-engineering-sites`
CLI and `config.yaml`, the tool configuration shared by the stack and the CLI
([design](https://github.com/jwallace145/jwall-engineering-infra/pull/26)).

| Path | Owns |
| --- | --- |
| `versions.tf`, `providers.tf`, `context.tf` | Terraform and provider constraints, region, identity, and the registry `context` module whose tags are applied through `default_tags` |
| `backend.tf`, `backend/prod.hcl` | S3 backend with native lockfile locking (no DynamoDB) |
| `state.tf` | The shared state bucket (`tfstate-bucket` module) |
| `oidc.tf` | GitHub OIDC provider (data source; owned by prog-strength-infra, see the TODO there) |
| `dns.tf` | The hosted zone, its CAA record and the zone owner's TXT records (`hosted-zone` module) |
| `ci-roles.tf` | This repo's own plan and apply roles (`ci-plan-role` and `ci-apply-role` modules) |
| `ecr.tf` | The ECR repository for the sites CLI image, configured from `config.yaml` at the repository root |
| `config.yaml` (root) | Tool configuration for the sites CLI and the foundation stack: Terraform version, library pin, state bucket, DNS zone, ECR lifecycle, sync headers |
| `src/jwall_engineering_sites/`, `tests/`, `pyproject.toml` | The sites CLI, its tests and its `uv` project |
| `stacks/site/` | The site stack the CLI renders for each site, pinned to the library like any consumer |
| `stacks/bot/` | The bot stack the CLI renders for each bot project: ECR repository, VPC, records bucket, bot instance |
| `src/jwall_engineering_sites/kinds/lichess_bot/` | The harness Dockerfile and entrypoint of the bot image |
| `bots.tf` | The `bots` map: one entry per bot project, each with plan, apply and release roles |
| `docs/lichess-bot.md`, `docs/consumer-note.md` | The bot projects guide and the note a bot repository links to |
| `Dockerfile` | The CLI image: a pinned Terraform, the stack with modules and providers vendored, `config.yaml` |
| `.github/actions/terraform-init` | `terraform init` with a retry for the registry's lag after a library release |
| `projects.tf` | The `projects` map: one entry per site, each fed to the registry `site-bucket`, `ci-plan-role`, `ci-preview-role` and `ci-apply-role` modules |
| `outputs.tf` | Values the project repos and this repo's CI consume |
| `docs/migration.md` | Legacy resources to retire after the move |
| `scripts/apply-repo-settings.sh` | GitHub repository settings and the `main` ruleset |

## State bucket and key convention

Every jwall-engineering stack keeps its state in `jwall-engineering-tfstate` under the key

```
project=<project>/env=<env>/<project>-<env>.tfstate
```

For example this stack uses `project=infra/env=prod/infra-prod.tfstate`, and the main site would use `project=main-site/env=prod/main-site-prod.tfstate`. Terraform writes the lock beside it as `<key>.tflock`. The bucket has versioning, SSE-S3, Block Public Access, `BucketOwnerEnforced` ownership, `prevent_destroy`, and a hardened bucket policy:

- each state prefix is reachable only by the CI roles listed for it (`state.tf`): a project's plan role reads `project=<key>/` and may write only `.tflock` objects there, its apply role reads and writes it, and this stack's key is limited to this repo's two roles;
- nothing outside those prefixes is reachable, and bucket configuration (the bucket policy included) cannot be changed, except by the break-glass principals in `local.bucket_admin_principal_arns`: the account root user (for browsing the buckets in the console), the `prog-strength-admin` user and this repo's apply role.

Every statement is a deny matched on `aws:PrincipalArn`, so it grants nothing: it stops any other principal in the account, including ones with `s3:*` on `*`, from reading or rewriting state. The statements are documented in the library's [`tfstate-bucket` README](https://github.com/jwallace145/terraform-aws-jwall-engineering/tree/main/modules/tfstate-bucket#hardened-bucket-policy).

## Projects registry

`local.projects` in `projects.tf` is the single list of sites:

| Key | Repository | Domain |
| --- | --- | --- |
| `main-site` | `jwallace145/site` | `jwall.engineering` |
| `cs-survival-guide` | `jwallace145/cs-survival-guide` | `survival-guide.jwall.engineering` |
| `guitar-knowledge-base` | `jwallace145/guitar-knowledge-base` | `guitar.jwall.engineering` |

Each entry is passed to the `site-bucket`, `ci-plan-role`, `ci-preview-role` and `ci-apply-role` submodules of the [registry module](#shared-modules-from-the-terraform-registry), which create:

- bucket `jwall-engineering-<key>` (versioned, encrypted, private, CloudFront-readable). Its policy lets only the project's apply role write `prod/` and `dev/` and only its preview role write `previews/`; only the break-glass principals write anything else or change the bucket's configuration. Reads are unrestricted, since the content is public through CloudFront. See the library's [`site-bucket` README](https://github.com/jwallace145/terraform-aws-jwall-engineering/tree/main/modules/site-bucket#hardened-bucket-policy).
- roles `jwall-engineering-<key>-ci-plan`, `-ci-preview`, and `-ci-apply`, trusted for the project's repository, each with managed policies from the library's `ci-policies` catalog scoped to that bucket, to the state prefix `project=<key>/`, and to Route 53 changes for `<domain>`, `*.preview.<domain>`, and the ACM validation records those names need:

| Role | Trusted for | Policies |
| --- | --- | --- |
| `jwall-engineering-<key>-ci-plan` | `pull_request` | `tfstate/read`, `static-site/read`, `ecr/pull` |
| `jwall-engineering-<key>-ci-preview` | `pull_request` | `static-site/previews`, `ecr/pull` |
| `jwall-engineering-<key>-ci-apply` | `environment:prod` | `tfstate/write`, `static-site/read`, `static-site/deploy`, `ecr/pull` |

The zone itself is owned here; every other record is owned by the project repos. `terraform output projects` lists the bucket name and role ARNs per project.

## Bot projects registry

`local.bots` in `bots.tf` is the single list of bot projects:

| Key | Repository |
| --- | --- |
| `rusty-chess` | `jwallace145/rusty-chess-v2` |

Each entry gets three roles. Their state is `project=<key>/` in the shared
bucket, readable by the plan role and writable by the apply role:

| Role | Trusted for | Policies |
| --- | --- | --- |
| `jwall-engineering-<key>-ci-plan` | `pull_request` on this repository | `tfstate/read`, `container/read` |
| `jwall-engineering-<key>-ci-apply` | `environment:prod` on this repository | `tfstate/write`, `container/read`, `container/manage` |
| `jwall-engineering-<key>-ci-release` | the bot repository's `v*` tags | `container/release` |

The bot's infrastructure is planned and applied here; the bot repository
holds only the project file and a release workflow, and its release role can
ship images and roll them out, nothing else. It cannot read the Lichess
token. `terraform output bots` lists the role ARNs.

## CI roles for this repository

| Role | Trusted for | Permissions |
| --- | --- | --- |
| `jwall-engineering-infra-ci-plan` | `pull_request` | `tfstate/read` (this stack's state key, write of its `.tflock` only), `foundation/read` |
| `jwall-engineering-infra-ci-apply` | `environment:prod` | `tfstate/write`, `foundation/read`, `foundation/manage`: everything needed to manage this stack, see below; `ecr/push` for the sites CLI image |

### Why the apply role is scoped the way it is

The policies come from the library's `ci-policies` catalog, whose README holds the full policy text and justification. In short, the apply role is the highest-privilege identity in the setup because it creates IAM roles, and it is constrained so that a compromised `prod` environment cannot reach beyond the `jwall-engineering-*` namespace:

- IAM actions are limited to roles and policies named `jwall-engineering-*`. It cannot touch any other principal, and it cannot modify the shared GitHub OIDC provider.
- `iam:AttachRolePolicy` only accepts a `jwall-engineering-*` customer-managed policy, so it cannot hand out `AdministratorAccess` to a role it creates. It can still edit the inline and customer-managed policies of `jwall-engineering-*` roles, itself included, so treat a compromise of `prod` as a compromise of the account.
- S3 access is limited to the state bucket's configuration (no `DeleteBucket`) and to the `jwall-engineering-*` buckets themselves, never their objects. The state bucket matches that pattern, so object access would have exposed every project's state; the role reaches only this stack's own state through `tfstate/write`.
- Route 53 access is limited to the `jwall.engineering` zone. It cannot create or delete zones.
- Neither role carries `ReadOnlyAccess`, which would grant `s3:GetObject` on every bucket, every project's state included. `foundation/read` grants the reads refresh and drift detection need for this stack.

Both roles trust only the `jwallace145/jwall-engineering-infra` repository through the OIDC provider, with the audience pinned to `sts.amazonaws.com`.

## Bootstrap (one time, run locally by the owner with admin credentials)

The state bucket does not exist before the first apply, so the first apply uses local state and is then migrated into the bucket. An override file (gitignored) swaps the backend to `local` for step 1 only.

1. **Apply with local state.**

   ```sh
   cd foundation
   export AWS_PROFILE=<admin profile>   # us-east-1, account 650503560686
   printf 'terraform {\n  backend "local" {}\n}\n' > backend_override.tf
   terraform init
   terraform plan -out=bootstrap.tfplan
   ```

   Review the plan. It should create the state bucket, this repo's two CI roles, the CAA record, the three project buckets and nine project CI roles, and **import** the hosted zone. The only changes on the zone should be tags and its comment. The GitHub OIDC provider is read, not imported. Then:

   ```sh
   terraform apply bootstrap.tfplan
   ```

2. **Migrate state into the bucket.** `backend.tf` and `backend/prod.hcl` are already in the repo.

   ```sh
   rm backend_override.tf
   terraform init -backend-config=backend/prod.hcl -migrate-state
   rm -f terraform.tfstate terraform.tfstate.backup bootstrap.tfplan
   ```

3. **Commit and push the initial scaffold.** This is the only direct push to `main`; every later change goes through a pull request.

   ```sh
   gh repo create jwallace145/jwall-engineering-infra --public --source=. --remote=origin --push
   ```

4. **Finish the hand-off** listed at the end of this file.

## CI/CD

**On pull requests** (`.github/workflows/ci.yml`):

- `Commit messages`: commitlint over every commit in `base..head`
- `PR title`: Conventional Commit check on the title
- `Terraform fmt, validate, tflint` (`foundation/`) and `Site stack validate, tflint` (`stacks/site/`)
- `Terraform plan`: assumes the plan role through OIDC and posts the foundation plan as the branded sticky PR comment (rendered by `jwall-engineering-sites report plan-comment`), updated on every push
- `Python lint, types, tests`: ruff, mypy and pytest for the sites CLI
- `Image build and smoke test`: builds the CLI image, runs `validate` inside it offline, checks its Terraform version and enforces the compressed-size budget
- `Bot stack validate, tflint` (`stacks/bot/`)
- `Bot plan (<project>)`: for each bot in `bots.tf`, assumes the bot's plan role and posts its plan as a sticky comment. It runs once the `AWS_ACCOUNT_ID` repository variable is set.

Every `terraform init` goes through `.github/actions/terraform-init`, which retries the registry's few-minute lag after a library release.

**Releases, applies and the image** (`.github/workflows/release-please.yml`, on push to `main`):

Merges to `main` accumulate in a Release Please PR. Merging that PR creates a tag; the `apply` job checks out the tag, assumes the apply role in the `prod` GitHub environment and runs `terraform apply`; then the `image` job, also in `prod`, builds the CLI image and pushes it to ECR as `jwall-engineering-sites:<tag>`. Nothing is applied to AWS without that human merge. The release bumps the package version in `src/jwall_engineering_sites/__init__.py` and the image tag the composite action runs (`.github/actions/jwall-engineering-sites/action.yml`); the reusable workflows carry no version, since they use the action from their own commit. `workflow_dispatch` with a `tag` re-publishes the image for an existing release.

Role ARNs are **repository variables**, not secrets: `AWS_PLAN_ROLE_ARN` and `AWS_APPLY_ROLE_ARN`. Bot roles are built from `AWS_ACCOUNT_ID` and the bot's name.

**Bot projects** (`.github/workflows/bot-apply.yml`, by hand): applies or destroys one bot's stack in the `prod` environment with its apply role. `.github/workflows/container-release.yml` is the reusable workflow a bot repository calls on each release ([guide](docs/lichess-bot.md#the-caller-workflow)).

## Local development

```sh
npm ci                                   # commitlint
pre-commit install --hook-type commit-msg --hook-type pre-commit
terraform fmt -check -recursive          # from the repository root
cd foundation
terraform init -backend=false && terraform validate
tflint --init && tflint --config ../.tflint.hcl --recursive
```

A real plan needs AWS credentials and the remote backend, from `foundation/`: `terraform init -backend-config=backend/prod.hcl && terraform plan`.

## Owner hand-off

Not automated; these need the owner's credentials or the GitHub UI.

**From the conventions:**

- Add repository secrets `RELEASE_BOT_CLIENT_ID` and `RELEASE_BOT_PRIVATE_KEY` (the full `.pem` contents).
- Install the `jwallace145-release-bot` GitHub App on this repo with Contents read/write and Pull requests read/write.
- After the first pull request is merged, run `scripts/apply-repo-settings.sh` to set merge methods, auto-delete of head branches, and the `main` ruleset.

**For this stack:**

- Run the bootstrap steps above.
- Create the GitHub `prod` environment with a deployment branch restriction to `main`.
- Set repository variables from `terraform output`: `AWS_PLAN_ROLE_ARN` and `AWS_APPLY_ROLE_ARN` here, and each project's role ARNs and bucket name (from `terraform output projects`) in that project's repo.
- Optional, for comments authored by the product rather than `github-actions[bot]`: create a GitHub App named **JWall Engineering Sites** (no webhook; permissions *Pull requests: read and write*; upload an avatar), generate a private key, and install it on this repo and every site repo. In each of those repos set the variable `JES_APP_CLIENT_ID` to the app's client ID and the secret `JES_APP_PRIVATE_KEY` to the full `.pem` contents, and pass them to the reusable workflows as shown above.
- Activate cost allocation tags in Billing: `Organization`, `Project`, `Environment`, `Repository`, `ManagedBy`, `Owner`, `Component`.
- For bot projects, after the release that creates their roles:
  - set the repository variable `AWS_ACCOUNT_ID` here;
  - set `AWS_RELEASE_ROLE_ARN` (from `terraform output bots`) in each bot repository;
  - run the **Bot apply** workflow;
  - set the token with `jwall-engineering-sites bot secrets set LICHESS_TOKEN`.

  The [guide](docs/lichess-bot.md#owner-setup-once) has the full list.
- Work through [docs/migration.md](docs/migration.md) to retire the legacy resources.

## License

[MIT](LICENSE)
