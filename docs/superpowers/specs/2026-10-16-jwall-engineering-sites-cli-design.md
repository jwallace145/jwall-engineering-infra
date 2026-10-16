# jwall-engineering-sites: a CLI that provisions and publishes the sites

Status: approved design, 2026-10-16. Guides the implementation PRs and releases
listed in [Rollout](#rollout).

## Summary

Today every site repository (`jwallace145/site`, `cs-survival-guide`,
`guitar-knowledge-base`) carries an `infra/` directory of nine Terraform files
and three workflows that are near-copies of each other. The directories differ
by four values; the workflows differ by the build command, the output
directory and some cache-control details. Everything else (backend config,
init, plan with a sticky comment, preview sync and cleanup, release plan and
apply, outputs, content sync, CloudFront invalidation) is duplicated
boilerplate that has already drifted.

This design replaces all of it with `jwall-engineering-sites`, a Python CLI
that lives in this repository, reads one YAML file from a site repository,
and drives Terraform and S3 from a versioned container image. Consumers keep
no Terraform. Their workflows shrink to a build job plus one `uses:` line
calling a reusable workflow published here. The infrastructure itself does
not change: the same registry modules, the same state bucket and key
convention, the same CI roles.

## Goals

- One YAML file per site repository is the whole interface. It is short,
  readable, validated, and contains only what differs between sites.
- Terraform, its state, the backend configuration and the module pins are
  hidden from site repositories and owned here.
- Plan, preview, apply and publish behave identically across every site, with
  preflight checks that fail fast and clearly, structured logs, and a debug
  log that is always available in GitHub Actions.
- Upgrades reach a site as one Dependabot PR bumping one version ref, whose
  CI plan shows exactly what changes.
- The tool is a thin, inspectable wrapper: it renders inputs and runs
  Terraform. It never carries infrastructure logic that belongs in the module
  library.

## Non-goals

- Changing the AWS resources a site consists of, or the state key convention.
- Running the site build. Builds stay in each repository's own CI, with its
  own toolchain, and hand the output to the tool.
- Managing the foundation stack in this repository with the CLI. It stays
  plain Terraform, moved into `foundation/`.
- Publishing the CLI anywhere but this account's private ECR. If it is ever
  distributed, it will be through a public package index, not ECR.
- A `destroy` command, or any pass-through of raw Terraform arguments.

## Decisions

| Decision | Choice | Why |
|---|---|---|
| Where the tool lives | This repository (`jwall-engineering-infra`). The module library `terraform-aws-jwall-engineering` stays separate. | The library stays a plain registry artifact the CLI pins like any consumer; a second module family later is just another stack the CLI renders. |
| Where Terraform runs | Bundled in the image. Site repositories hold no `.tf` files. | The only option that hides Terraform and state. Upgrades become image bumps instead of nine-pin bumps. |
| Who builds the site | The site repository's CI. The CLI takes a built directory. | The image stays small and toolchain-free; the build is the one genuinely site-specific step. |
| Image registry | Private ECR, `jwall-engineering-sites`. | The owner does not want other consumers pulling from this account. |
| Consumer interface | Reusable workflows (`workflow_call`) built on a composite action. | Maximum standardisation: plan comments, preview comments, release-please and cleanup live here once. |
| Version pinning | Exact tags (`@v0.5.0`), bumped by Dependabot. | Same discipline as module pins today. A bad release cannot reach a site until its bump PR is merged. |
| CLI name | `jwall-engineering-sites` | Matches the ECR repository name and the `jwall-engineering-*` IAM scope. |
| Tool configuration | `config.yaml` at this repository's root, read by the CLI and by the foundation stack (`yamldecode`). | One top-level knob for everything about how the tool runs; YAML so Terraform can read it without a generated copy. |

## Architecture

```
site repo                       this repo (jwall-engineering-infra)                   AWS
---------                       ----------------------------------                    ---
jwall-engineering-sites.yaml    config.yaml ─┐                                        S3 state bucket
build job ──artifact──┐         stacks/site/ ├─> image vX.Y.Z (ECR) ──docker run──>  site bucket
workflow: uses site-*.yml@vX ───reusable workflow ──composite action──┘               CloudFront, Route 53
                                foundation/  (ECR repository, CI roles, buckets, zone)
```

A run: the reusable workflow checks out the site repo, downloads the build
artifact, assumes the site's CI role, logs in to ECR and runs the image with
the workspace mounted. The CLI loads `config.yaml` (baked into the image) and
`jwall-engineering-sites.yaml` (from the workspace), derives everything else,
renders `stacks/site/` inputs into a temporary directory, and runs Terraform
and boto3.

## Repository layout

```
jwall-engineering-infra/
  config.yaml                       tool configuration (see below)
  foundation/                       the existing Terraform stack, moved as-is
  stacks/site/                      the site-stack root config the CLI drives
  src/jwall_engineering_sites/      the Python package; console script jwall-engineering-sites
  tests/                            pytest
  Dockerfile
  pyproject.toml, uv.lock
  .github/workflows/
    ci.yml, release-please.yml      existing, extended
    site-plan.yml                   reusable (workflow_call)
    site-preview.yml                reusable
    site-release-please.yml         reusable
    site-deploy.yml                 reusable
  .github/actions/jwall-engineering-sites/action.yml   composite action
  docs/superpowers/specs/           this document
```

Moving the foundation stack to `foundation/` is a file move. Its backend
configuration is passed explicitly (`-backend-config=backend/prod.hcl`), so
state is untouched; the existing workflows gain `working-directory:
foundation`.

Package: distribution `jwall-engineering-sites`, import
`jwall_engineering_sites`, command `jwall-engineering-sites`. Python 3.13,
managed with `uv`. Dependencies: `typer`, `pydantic`, `pyyaml`, `boto3`.
Development: `pytest`, `moto`, `ruff`, `mypy`.

## Configuration

### `config.yaml` (tool, this repository)

```yaml
tool:
  log_level: info            # JES_LOG_LEVEL overrides, for local debugging
  log_format: console        # json in CI

terraform:
  version: 1.14.7
  lock_timeout: 120s
  plan_timeout: 15m

library:
  source: jwallace145/jwall-engineering/aws
  version: 0.7.0             # the one pin the whole fleet shares

state:
  bucket: jwall-engineering-tfstate
  region: us-east-1
  key_template: project={project}/env={environment}/{project}-{environment}.tfstate

context:
  organization: jwall-engineering
  owner: jwallace145

dns:
  zone: jwall.engineering
  zone_id: Z029546536VUFGDCPXZ21

ecr:
  repository: jwall-engineering-sites
  keep_release_images: 20
  expire_untagged_after_days: 7

sync:
  cache_control: public, max-age=3600
  preview_cache_control: public, max-age=300
  immutable_cache_control: public, max-age=31536000, immutable

plan:
  comment_max_bytes: 60000
```

- Baked into the image at `/etc/jwall-engineering-sites/config.yaml`. The
  CLI reads `JES_CONFIG` if set, else that path, else `./config.yaml` (for
  running from this repository locally).
- Loaded once into a Pydantic `Settings` model; every command receives it.
  Unknown keys are rejected. `jwall-engineering-sites config show` prints the
  effective settings.
- A small allowlist of environment overrides, all prefixed `JES_`:
  `JES_LOG_LEVEL`, `JES_LOG_FORMAT`, `JES_SKIP_ROLE_CHECK` (local use only,
  logged as a warning).
- The foundation stack reads the same file:
  `yamldecode(file("${path.module}/../config.yaml"))`, for the ECR repository
  name and lifecycle values. One source of truth, no generated file.
- `library.version` is bumped by `chore(deps)` PRs here; Dependabot's
  `terraform` ecosystem watches `stacks/site/` for it.

### `jwall-engineering-sites.yaml` (site, each site repository root)

```yaml
version: 1
project: guitar-knowledge-base            # bucket, roles, state key and tags derive from this
repository: jwallace145/guitar-knowledge-base
domain: guitar.jwall.engineering

tags:                                     # applied to every resource through default_tags
  Organization: jwall-engineering         # these six are required and checked against the
  Project: guitar-knowledge-base          # fields above; extra keys are allowed and passed
  Environment: prod                       # through unchanged
  Repository: jwallace145/guitar-knowledge-base
  ManagedBy: terraform
  Owner: jwallace145

site:
  output_dir: site                        # built by the repository's CI, handed over as an artifact
  immutable_paths: []                     # e.g. ["_astro/**"]: hashed assets, cached for a year

preview:
  enabled: true                           # pr-<n>.preview.<domain>

dev:                                      # optional second environment
  domain: dev.guitar.jwall.engineering
```

Validation (Pydantic, unknown keys rejected, errors name the file and field):

| Field | Rule |
|---|---|
| `version` | must be `1` |
| `project` | kebab-case, at most 30 characters (same rule as the `context` module) |
| `repository` | `owner/repo` |
| `domain`, `dev.domain` | lowercase FQDN under `dns.zone`, no trailing dot |
| `site.output_dir` | relative path, no `..` |
| `site.immutable_paths` | glob patterns relative to `output_dir` |
| `preview.enabled` | bool, default `true` |
| `tags` | required map. The six required keys must all be present (`Organization`, `Project`, `Environment`, `Repository`, `ManagedBy`, `Owner`); `Organization` must equal `context.organization`, `Project` must equal `project`, `Environment` must be `prod`, `Repository` must equal `repository`, `ManagedBy` must be `terraform`. Extra keys are allowed. Missing or mismatched required tags fail fast with exit code 2, before any AWS call. |

Derived values (never configurable in the site file):

| Value | Derivation |
|---|---|
| Bucket | `<context.organization>-<project>` |
| CI roles | `<context.organization>-<project>-ci-{plan,preview,apply}` |
| State key | `state.key_template` with `environment = prod` |
| Preview prefix and URL | `previews/pr-<n>/`, `https://pr-<n>.preview.<domain>/` |
| Tags | `tags` from the site file, passed to the stack's `default_tags`; the stack's `tags` variable validates the six required keys again, so Terraform enforces them even if the CLI is bypassed |

The file is read from the working directory; `--file` overrides the path.

## The bundled site stack (`stacks/site/`)

What the three `infra/` directories are today, with every site-specific
value turned into a variable: `project`, `repository`, `domain_name`,
`dev_domain_name`, `enable_preview`, `zone_id` and `tags`. `tags` feeds the
provider's `default_tags` directly and carries a validation that the six
required keys are present, so the stack enforces the tagging contract on its
own. It calls the library root module, pinned to `library.version`; the
`context` module is no longer needed because the site file states the tags
explicitly. `backend "s3" {}` is empty; the CLI supplies the backend
configuration. Its outputs are what the CLI needs: `bucket_name`,
`prod_url`, `prod_distribution_id`, `dev_url`, `dev_distribution_id`,
`preview_url_pattern`, `preview_distribution_id`.

At image build time the stack is initialised offline (`terraform init
-backend=false`) so `.terraform/modules` holds the library, and providers are
mirrored with `terraform providers mirror`. At run time the CLI copies the
stack into a temporary directory, writes `terraform.tfvars.json` and
`backend.hcl`, and runs `terraform init -input=false -plugin-dir=<mirror>
-backend-config=backend.hcl`. Only the S3 backend touches the network.

A test renders the three real site files (fixtures) and asserts the rendered
`tfvars` equal what each repository's `infra/` directory produces today.

## The CLI

### Commands

| Command | Expected role | Behaviour |
|---|---|---|
| `validate` | none | Load and validate both files, render the stack inputs, `terraform validate` offline. No credentials. |
| `plan` | `ci-plan` | Init, `terraform plan -detailed-exitcode`. Writes `plan.txt`, `plan-comment.md`, and `changes=true|false` plus `result=no-changes|changes|failed` to `GITHUB_OUTPUT`. Exit 0 whether or not there are changes. |
| `preview deploy --pr N --dist DIR` | `ci-preview` | Sync `DIR` to `previews/pr-N/` with `sync.preview_cache_control`; print the URL; write `preview-comment.md`. |
| `preview remove --pr N` | `ci-preview` | Delete `previews/pr-N/`. Idempotent. |
| `apply` | `ci-apply` | Init, `terraform plan -out`, `terraform apply` of that saved plan, write `outputs.json`. |
| `publish --dist DIR [--environment prod\|dev]` | `ci-apply` | Sync to `prod/` (or `dev/`) with `sync.cache_control`, invalidate `/*` on that environment's distribution, write the step summary. |
| `release --dist DIR` | `ci-apply` | `apply` then `publish --environment prod`. What `site-deploy.yml` calls. |
| `outputs` | `ci-plan` | Stack outputs as JSON. |
| `config show` | none | Effective tool settings. |

All run artifacts (`plan.txt`, `plan-comment.md`, `preview-comment.md`,
`outputs.json`, the debug log) are written to `./.jwall-engineering-sites/`
in the workspace. Site repositories add it to `.gitignore`.

Exit codes: `0` success, `1` Terraform or AWS failure, `2` configuration or
validation error, `3` preflight failure. Distinct codes make the failing
layer obvious from the job summary.

### Preflight checks (before Terraform or S3 is touched)

- `sts:GetCallerIdentity` must resolve to an assumed-role session of the role
  the command expects, by name. A mismatch is a one-line error naming the
  expected and actual role. `JES_SKIP_ROLE_CHECK=1` disables it for local
  runs with admin credentials and logs a warning.
- `--dist` must exist, be a directory, and contain `index.html`.
- `--pr` must be a positive integer.
- The derived bucket must exist (`HeadBucket`).
- A held state lock is reported with the lock's holder, operation and
  creation time (parsed from Terraform's error), then retried until
  `terraform.lock_timeout`.

### Terraform execution

`TF_IN_AUTOMATION=1`, `TF_INPUT=0`, `-no-color`, `-lock-timeout` from config,
`plan_timeout` enforced by the runner. Output streams to the log as it is
produced inside a `::group::` per phase (preflight, init, plan, apply, sync,
invalidate) and is captured for the comment file, truncated to
`plan.comment_max_bytes` with a note when truncated. The temporary working
directory is removed on success and kept, with its path logged, on failure
when the log level is `debug`.

### S3 sync (boto3; no AWS CLI in the image)

1. Walk `--dist`; for each file compute MD5, content type (`mimetypes` plus an
   explicit table for `.webmanifest`, `.wasm`, `.woff2`, `.avif`, `.svg`) and
   cache-control (`immutable_cache_control` if the relative path matches
   `site.immutable_paths`, else the environment's value).
2. List the target prefix; compare MD5 to ETag. Uploads are single-part
   `PutObject`, so ETag equals MD5.
3. Upload changed and new files, non-HTML first and HTML last, so a page
   never references an asset that is not yet there. Then delete remote keys
   with no local file.
4. Report counts: uploaded, unchanged, deleted, bytes.

The sync planner (local listing + remote listing → upload, skip and delete
sets with per-file headers) is a pure function, unit-tested without AWS.

### Logging and debuggability

- Console at `tool.log_level`, `console` or `json` format.
- A debug-level log file is always written to
  `./.jwall-engineering-sites/jwall-engineering-sites.log`: every subprocess
  command line, exit code and duration, the rendered `tfvars` and backend
  config, every S3 operation. Nothing secret is logged; credentials are never
  rendered.
- When `GITHUB_ACTIONS` is set: `::group::` per phase, `::error::` with the
  failing phase and command, `ACTIONS_STEP_DEBUG` (or the workflows' `debug`
  input) raises the console to `debug`, and the step summary gets the short
  version: role, image version, plan result or deploy counts, URLs.
- The reusable workflows upload `./.jwall-engineering-sites/` as an artifact
  on every run (`if: always()`, 14-day retention).

## The image

- `python:3.13-slim`, `linux/amd64`. Terraform `terraform.version`
  downloaded with checksum verification. The package installed from
  `uv.lock`. `config.yaml` at `/etc/jwall-engineering-sites/`. `stacks/site/`
  with modules fetched and providers mirrored at `/opt/jwall-engineering-sites/`.
- Entry point `jwall-engineering-sites`. Working directory `/work`; the
  workspace is mounted there.
- Tagged exactly `vX.Y.Z`. The ECR repository has tag immutability on, so
  there is no `latest`. The workflow ref a site pins and the image tag it
  runs are the same string.
- Built on every pull request (not pushed) and smoke-tested by running
  `validate` inside the container against a fixture site file.

## GitHub Actions

### Composite action `.github/actions/jwall-engineering-sites`

Inputs: `command` (required), `role-arn` (required), `args` (default empty),
`debug` (default `false`). Steps: `aws-actions/configure-aws-credentials`
with `role-arn`, `aws-actions/amazon-ecr-login`, then `docker run --rm` with
the workspace mounted at `/work`, the `AWS_*` credential environment passed
through, and `GITHUB_OUTPUT` and `GITHUB_STEP_SUMMARY` mounted so the tool
can write to them.

The image tag is a literal in `action.yml`, annotated
`# x-release-please-version`, rewritten by Release Please on each release.

### Reusable workflows

Relative `uses: ./...` inside a called workflow resolves against the
caller's checkout, so the reusable workflows reference the composite action
by full path and tag, `jwallace145/jwall-engineering-infra/.github/actions/jwall-engineering-sites@vX.Y.Z`,
also annotated for Release Please.

Role ARNs are explicit inputs rather than read from the `vars` context, so
the contract is visible in the caller.

| Workflow | Jobs | Inputs | Caller permissions |
|---|---|---|---|
| `site-plan.yml` | `validate` (no credentials, always runs; the required check) and `plan` (skipped for forks and `edited` events), sticky comment with header `terraform-plan` | `plan-role-arn`, `debug` | `contents: read`, `id-token: write`, `pull-requests: write` |
| `site-preview.yml` | opened/synchronize/reopened: download the artifact, `preview deploy`, sticky comment `preview`; closed: `preview remove`, update the comment | `artifact`, `preview-role-arn`, `debug` | same |
| `site-release-please.yml` | Release Please with the bot app token; outputs `release_created`, `tag` | none; `secrets: inherit` | `contents: write`, `pull-requests: write` |
| `site-deploy.yml` | `environment: prod` (the caller's environment), download the artifact built at the tag, `release --dist`, step summary | `artifact`, `tag`, `apply-role-arn`, `debug` | `contents: read`, `id-token: write` |

Every job uploads `./.jwall-engineering-sites/` with `if: always()`.

### What a site repository's workflows become

```yaml
# preview.yml
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
    uses: jwallace145/jwall-engineering-infra/.github/workflows/site-preview.yml@v0.5.0
    with:
      artifact: site
      preview-role-arn: ${{ vars.AWS_PREVIEW_ROLE_ARN }}
    permissions: { contents: read, id-token: write, pull-requests: write }
```

`ci.yml` is the site's own build and tests plus a `site-plan.yml` call.
`release.yml` is a `site-release-please.yml` call, a build job at the
released tag, and a `site-deploy.yml` call. The build job is the only part
that differs between sites. Commitlint and PR-title jobs stay in each
repository as they are.

## Library changes (`terraform-aws-jwall-engineering`, release 0.8.0)

- New module `ecr-repository`: `name`, image tag immutability, scan on push,
  AES256 encryption, a lifecycle policy with `keep_release_images` (tagged
  `v*`) and `expire_untagged_after_days`, `Component` tag. Outputs `arn`,
  `url`, `name`.
- Catalog: a new class `common` that every role accepts (plan: read, common;
  preview: preview, common; apply: read, write, common). Two policies:
  - `ecr/pull` (common): `ecr:GetAuthorizationToken` on `*` (no resource
    support) and `ecr:BatchGetImage`, `ecr:GetDownloadUrlForLayer`,
    `ecr:BatchCheckLayerAvailability` on the repository. Inputs:
    `policy_inputs.ecr = { repository_arn }`.
  - `ecr/push` (write): `ecr:GetAuthorizationToken` on `*`; `ecr:PutImage`,
    `ecr:InitiateLayerUpload`, `ecr:UploadLayerPart`,
    `ecr:CompleteLayerUpload`, `ecr:BatchCheckLayerAvailability`,
    `ecr:BatchGetImage` on the repository.
- `foundation/read` gains `ecr:Describe*`, `ecr:List*`,
  `ecr:GetLifecyclePolicy`, `ecr:GetRepositoryPolicy` on
  `repository/<name_root>-*`; `foundation/manage` gains create, delete, tag,
  lifecycle-policy and repository-policy actions on the same ARN pattern.

## Foundation changes (this repository)

- `foundation/ecr.tf`: `module "sites_image"` from `ecr-repository`, name and
  lifecycle values from `config.yaml` via `yamldecode`.
- `foundation/ci-roles.tf`: the infra apply role adds `ecr/push` with
  `policy_inputs.ecr.repository_arn = module.sites_image.arn`.
- `foundation/projects.tf`: each project's plan, preview and apply roles add
  `ecr/pull` with the same input.

## Release pipeline

Release Please switches from the `simple` to the `python` release type so it
bumps `pyproject.toml`; `extra-files` cover the two annotated literals (the
composite action's image tag and the reusable workflows' action ref). On a
release:

1. `Terraform apply` of `foundation/` as today, which also creates or
   updates the ECR repository.
2. A new `image` job, `needs: apply`, builds the image with
   `docker/build-push-action` and pushes `vX.Y.Z` using the infra apply role
   (which has `ecr/push`).
3. Dependabot opens the workflow-ref bump PR in each site repository.

Pull-request CI adds: `uv run ruff check`, `uv run ruff format --check`,
`uv run mypy`, `uv run pytest`, `terraform fmt/validate/tflint` for
`stacks/site/` and `foundation/`, and the image build plus smoke test.

## Rollout

Each step is a release on its own and leaves every site working.

| Step | Repository | PR | Acceptance |
|---|---|---|---|
| 1 | library | `feat(ci-policies): add ecr-repository and the ecr/pull and ecr/push policies` (0.8.0) | Tests pass; existing catalog tests unchanged. |
| 2 | this repo | `feat(ecr): add the jwall-engineering-sites repository and ECR policies` (plain Terraform; bump pins to 0.8.0; `config.yaml` introduced for the ECR values) | CI plan: one repository, policy updates on the ten roles that pull or push (nine project roles, the infra apply role), nothing else. Release applies it. |
| 3 | this repo | `refactor: move the foundation stack to foundation/` | CI plan: no changes. |
| 4 | this repo | `feat(cli): add the jwall-engineering-sites package` (package, `config.yaml` completed, `stacks/site/`, tests, Dockerfile, CI) | `pytest`, image build and smoke test green; release pushes the first image. |
| 5 | this repo | `feat(actions): add the composite action and reusable site workflows` (Release Please switched to `python` with `extra-files`) | A release rewrites both annotated literals and pushes an image with the same tag. |
| 6 | `guitar-knowledge-base` | `feat(ci): manage infrastructure with jwall-engineering-sites` (delete `infra/`, add `jwall-engineering-sites.yaml`, replace workflows, `.gitignore`, Dependabot) | **The CI plan on the PR shows no changes.** A preview deploys and is cleaned up. The next release applies and publishes. |
| 7 | `cs-survival-guide`, `site` | same as 6 (`site` sets `immutable_paths: ["_astro/**"]`) | same |
| 8 | this repo | `docs: describe the sites CLI` (README, CONTRIBUTING, AGENTS) | — |

Step 6's "no changes" plan is the acceptance test for the refactor: the tool
renders the same module version, inputs and state key the repository had, so
Terraform must see nothing to do.

## Testing

- Python (`pytest`, `ruff`, `mypy`): every validation rule of both config
  models, with the three real site files as fixtures; every derivation
  (bucket, roles, state key, URLs); the sync planner as a pure function and
  the S3 round trip with `moto`; the Terraform runner against a fake
  `terraform` on `PATH` that records its arguments; each command through
  Typer's `CliRunner` with the preflight checks mocked; the preflight checks
  themselves against stubbed STS and S3 responses.
- Terraform: `stacks/site/` validates offline; the fixture rendering test
  described above.
- Image: built in CI on every PR and smoke-tested with `validate` inside the
  container, which proves the vendored modules and providers work offline.
- Reusable workflows: exercised for real in step 6.

## Risks

| Risk | Mitigation |
|---|---|
| The rendered stack differs from a site's `infra/` and the migration plan shows changes | The fixture rendering test catches it before any site migrates; step 6 merges only on a no-changes plan. |
| The infra apply role lacks an ECR action and the image push fails | Step 2 lands the policies first; the first push happens in step 4's release, with the admin user as fallback. |
| ETag comparison is wrong for an object uploaded by an earlier multipart `aws s3 sync` | The planner treats a non-MD5 ETag as changed and re-uploads once; after that every object is single-part. |
| A reusable workflow change breaks all three sites at once | Exact pins: nothing reaches a site until its Dependabot PR is merged, and that PR's plan shows the effect. |
| Local debugging needs admin credentials but the role check refuses them | `JES_SKIP_ROLE_CHECK=1`, logged as a warning. |

## Out of scope, for later

- Driving the foundation stack with the CLI.
- Pinning each site bucket's CloudFront access to its own distributions.
- Publishing the CLI to a public package index.
- A `dev` environment workflow (`publish --environment dev` exists, but no
  reusable workflow calls it yet).
