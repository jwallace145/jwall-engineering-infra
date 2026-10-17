# Guidance for coding agents

This repository is Terraform for the jwall-engineering foundation stack (state bucket, GitHub OIDC provider, `jwall.engineering` hosted zone, per-project buckets and CI roles). Read [README.md](README.md) for the layout and [CONTRIBUTING.md](CONTRIBUTING.md) for the full rules. The rules below are the ones you MUST follow.

## Rules

- **Never push to `main`.** Every change goes through a pull request on a branch. There are no exceptions after the initial scaffold.
- **Open PRs with a typed template** and fill in every section:

  ```sh
  gh pr create --template <feat|fix|docs|chore|refactor|ci>.md
  ```

  The PR title MUST be a valid Conventional Commit; squash merges use it as the commit message.
- **Commits are a single line**: `type(scope)?: subject`, at most 100 characters. Allowed types: `feat`, `fix`, `docs`, `chore`, `refactor`, `ci`, `build`, `perf`, `style`, `test`, `revert`. The only thing allowed after the header is a blank line and `Co-authored-by: Name <email>` trailers. No body text. Put details in the PR description.
- **Merges are squash or rebase only.** Never create merge commits.
- **Do not modify or delete existing AWS resources** that this stack does not own. The legacy resources listed in `docs/migration.md` are retired by the owner, not by Terraform in this repo.
- **Never run `terraform apply` yourself.** Applies happen only through the Release Please `apply` job. Local plans are fine with read-only credentials. The same goes for the sites CLI: never run `jwall-engineering-sites apply`, `publish` or `release` against a real site; its role check refuses non-CI credentials unless `JES_SKIP_ROLE_CHECK=1` is set, and you do not set it.
- **Do not add `ignore_changes`, `-lock=false`, or widen an IAM policy** to make a plan pass. Explain the diff in the PR instead.
- **Shared modules come from the Terraform Registry.** Every resource in this stack comes from a submodule of `jwallace145/jwall-engineering/aws`, pinned to an exact `version` in each module block. Bump all pins together (`foundation/`, `stacks/site/main.tf` and `library.version` in `config.yaml`), never switch a module back to a git or local source, and fix module bugs upstream in `jwallace145/terraform-aws-jwall-engineering` rather than working around them here.
- **Version literals are Release Please's.** `__version__` in `src/jwall_engineering_sites/__init__.py` and the image tag in `.github/actions/jwall-engineering-sites/action.yml` carry `# x-release-please-version` and are bumped by releases; never edit them by hand and never put one under `.github/workflows/`. The reusable workflows use the action from their own commit (`github.job_workflow_sha`).
- **Keep secrets out of the repo and out of variables.** Role ARNs are repository variables; the release bot credentials are repository secrets managed by the owner.

## Before opening a PR

```sh
terraform fmt -recursive
(cd foundation && terraform init -backend=false && terraform validate)
(cd stacks/site && terraform init -backend=false && terraform validate)
tflint --init && tflint --recursive
npm ci && npm test
uv sync && uv run ruff format --check && uv run ruff check && uv run mypy && uv run pytest
```

- **The sites CLI lives in `src/jwall_engineering_sites/`** with tests in `tests/`. Python 3.13, `uv`, Typer, Pydantic. `config.yaml` at the root is the tool configuration; `stacks/site/` is the Terraform the CLI renders per site. Keep `tests/fixtures/config.yaml` identical to `config.yaml` (a test checks).

## Things that need the owner

Anything in the "Owner hand-off" section of README.md: AWS admin credentials, GitHub secrets, the `prod` environment, repository variables, repository settings, and Billing tags. List them in the PR instead of attempting them.
