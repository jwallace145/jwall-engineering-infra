# Contributing

This repo follows the jwall-engineering conventions. The short version: one-line Conventional Commits, every change through a pull request, squash or rebase merges only, and releases cut by Release Please.

## Workflow

1. Branch from `main`.
2. Make the change. Keep Terraform formatted (`terraform fmt`) and run the local checks below.
3. Open a PR with a typed template (see below). The PR title must be a valid Conventional Commit.
4. Review the `Terraform plan` comment CI posts on the PR. It is updated on every push.
5. Merge with **squash** or **rebase**. Merge commits are disabled and history stays linear.

Direct pushes to `main` are not allowed. The only exception was the initial scaffold commit during bootstrap.

## Commit messages: one line, nothing else

Every commit message is a single line:

```
type(scope)?: subject
```

- At most 100 characters.
- `type` is one of `feat`, `fix`, `docs`, `chore`, `refactor`, `ci`, `build`, `perf`, `style`, `test`, `revert`.
- Breaking changes are marked with `!`, for example `feat!: move state key`.
- The only content allowed after the header is a blank line followed by `Co-authored-by: Name <email>` trailers.
- No body text. Details belong in the PR description.

commitlint enforces this with `@commitlint/config-conventional` plus a local rule, `only-coauthor-trailers` (`tools/commitlint/only-coauthor-trailers.mjs`), which fails on any line after the header that is not blank or a `Co-authored-by` trailer. CI runs it over every commit in the PR range, so a rebase merge never lands a non-conforming commit on `main`.

### Why the PR title matters

Squash merges use the PR title as the commit message on `main`, and Release Please reads those messages to decide the next version and to write `CHANGELOG.md`. A `feat` bumps the minor version, a `fix` bumps the patch, and `!` bumps the major. CI lints the title with the same rules.

### Dependabot PRs

Dependabot cannot write one-line, lowercase commit messages, so its PRs skip the commit message check and the title check. Merge them with **squash** and a lowercase Conventional Commit subject, for example:

```sh
gh pr merge <n> --squash --subject "ci: bump actions/checkout from 6 to 7"
```

The CI plan comment still has to show no changes.

## Pull request templates

Templates live in `.github/PULL_REQUEST_TEMPLATE/`, one per type: `feat.md`, `fix.md`, `docs.md`, `chore.md`, `refactor.md`, `ci.md`. Pick one with

```sh
gh pr create --template fix.md
```

or by appending `?template=fix.md` to the compare URL. Every template has Summary, Motivation and context, What changed, How it was verified, and a Checklist. `fix.md` adds Bug, Root cause, and Reproduction. Fill in every section.

## Local setup

Requirements: Terraform `>= 1.11`, [tflint](https://github.com/terraform-linters/tflint), Node.js `>= 22`, and [pre-commit](https://pre-commit.com).

```sh
npm ci
pre-commit install --hook-type commit-msg --hook-type pre-commit
terraform init -backend=false
tflint --init
```

The `commit-msg` hook runs commitlint on every commit; the `pre-commit` hook runs `terraform fmt`, `terraform validate`, and `tflint`. CI is the authoritative check; the hooks exist so mistakes are caught before they are pushed. Skip them once with `git commit --no-verify` if you must, and expect CI to object.

### Checks that must pass

| Check | Runs |
| --- | --- |
| `Commit messages` | commitlint over `base..head` and the rule's unit tests (`npm test`) |
| `PR title` | Conventional Commit check on the title |
| `Terraform fmt, validate, tflint` | `terraform fmt -check`, `terraform validate`, `tflint --recursive` |
| `Terraform plan` | plan through the OIDC plan role, posted as a PR comment |

A plan that touches the imported hosted zone with anything other than tags needs a clear explanation in the PR.

## Updating the shared modules

The `context`, `tfstate-bucket`, `hosted-zone`, `infra-ci-roles`, `site-bucket`, and `site-ci-roles` modules come from the public Terraform Registry module [`jwallace145/jwall-engineering/aws`](https://registry.terraform.io/modules/jwallace145/jwall-engineering/aws). Bump all six `version` pins together (`context.tf`, `state.tf`, `dns.tf`, `ci-roles.tf`, and two in `projects.tf`), run `terraform init -upgrade` locally, and open a `chore(deps)` PR. Dependabot does the same automatically. Review the library's changelog entry alongside the CI plan comment before merging. A change to a module's inputs or outputs belongs in the library repo, [`jwallace145/terraform-aws-jwall-engineering`](https://github.com/jwallace145/terraform-aws-jwall-engineering), not here.

## Releases

Release Please opens and maintains a release PR as changes land on `main`. Merging that PR:

1. tags a new version (starting at `v0.1.0`) and updates `CHANGELOG.md`;
2. triggers the `apply` job, which checks out the tag, assumes the apply role in the `prod` environment, and runs `terraform apply`.

Merging the release PR is the deliberate act that changes AWS. Merge it when the accumulated plan is what you want applied. Release PRs are authored by `jwallace145-release-bot[bot]` so that CI runs on them.

## Project layout

See the Layout table in [README.md](README.md).
