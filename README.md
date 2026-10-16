# jwall-engineering-infra

The foundation stack for every [jwall.engineering](https://jwall.engineering) site. It owns:

- the shared Terraform state bucket `jwall-engineering-tfstate`
- a reference to the account's GitHub Actions OIDC identity provider (owned by prog-strength-infra for now)
- the `jwall.engineering` Route 53 hosted zone (zone-level records only)
- for each site in the `projects` registry, an S3 bucket and three CI roles
- its own plan and apply CI roles

Everything is in `us-east-1`, managed with Terraform `>= 1.11` and the AWS provider `~> 6.0`. The shared building blocks are consumed from the public Terraform Registry, see [Shared modules](#shared-modules-from-the-terraform-registry). Releases are cut and applied by [Release Please](#releases-and-applies). Contribution rules are in [CONTRIBUTING.md](CONTRIBUTING.md); the rules for coding agents are in [AGENTS.md](AGENTS.md).

## Shared modules from the Terraform Registry

This stack is a consumer of [`jwallace145/jwall-engineering/aws`](https://registry.terraform.io/modules/jwallace145/jwall-engineering/aws), the jwall-engineering module library published to the public Terraform Registry from [`jwallace145/terraform-aws-jwall-engineering`](https://github.com/jwallace145/terraform-aws-jwall-engineering). The library is open source under MIT, versioned with semver, and released with the same Release Please flow as this repo. Consuming it here, the same way any third party would, is deliberate: the foundation stack eats its own dog food, so the modules are exercised in production before anyone else depends on them.

| Submodule | Used for | Where |
| --- | --- | --- |
| `//modules/context` | The tag set applied to every resource through `default_tags` | `context.tf` |
| `//modules/tfstate-bucket` | The shared Terraform state bucket | `state.tf` |
| `//modules/hosted-zone` | The `jwall.engineering` zone and its CAA record | `dns.tf` |
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
  version  = "0.8.1"

  project = each.key
}
```

Versions are pinned exactly, not with `~>`, because Terraform records provider versions in `.terraform.lock.hcl` but not module versions; an exact pin is the only way two applies of the same commit resolve the same code. The ten pins (`context.tf`, `state.tf`, `dns.tf`, `ecr.tf`, two in `ci-roles.tf`, and four in `projects.tf`) are always bumped together, and so is `library.version` in `config.yaml`.

**Upgrading.** When the library publishes a release, Dependabot's Terraform ecosystem opens a `chore(deps)` PR bumping the pins. The CI plan comment on that PR shows exactly what the new version changes in AWS. Read the library's [CHANGELOG](https://github.com/jwallace145/terraform-aws-jwall-engineering/blob/main/CHANGELOG.md) entry, check the plan, merge, and the change applies with the next release of this repo. While the library is on `0.x`, a breaking change bumps the minor version and is marked `!` in its changelog.

## Layout

| Path | Owns |
| --- | --- |
| `versions.tf`, `providers.tf`, `context.tf` | Terraform and provider constraints, region, identity, and the registry `context` module whose tags are applied through `default_tags` |
| `backend.tf`, `backend/prod.hcl` | S3 backend with native lockfile locking (no DynamoDB) |
| `state.tf` | The shared state bucket (`tfstate-bucket` module) |
| `oidc.tf` | GitHub OIDC provider (data source; owned by prog-strength-infra, see the TODO there) |
| `dns.tf` | The hosted zone and its CAA record (`hosted-zone` module) |
| `ci-roles.tf` | This repo's own plan and apply roles (`ci-plan-role` and `ci-apply-role` modules) |
| `ecr.tf`, `config.yaml` | The ECR repository for the sites CLI image, and the tool configuration it and the CLI read |
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
- `Terraform fmt, validate, tflint`
- `Terraform plan`: assumes the plan role through OIDC and posts the plan as a PR comment that is updated on every push

**Releases and applies** (`.github/workflows/release-please.yml`, on push to `main`):

Merges to `main` accumulate in a Release Please PR. Merging that PR creates a tag, and the `apply` job then checks out the tag, assumes the apply role in the `prod` GitHub environment, and runs `terraform apply`. Nothing is applied to AWS without that human merge.

Role ARNs are **repository variables**, not secrets: `AWS_PLAN_ROLE_ARN` and `AWS_APPLY_ROLE_ARN`.

## Local development

```sh
npm ci                                   # commitlint
pre-commit install --hook-type commit-msg --hook-type pre-commit
terraform init -backend=false
terraform fmt -check -recursive && terraform validate
tflint --init && tflint --recursive
```

A real plan needs AWS credentials and the remote backend: `terraform init -backend-config=backend/prod.hcl && terraform plan`.

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
- Activate cost allocation tags in Billing: `Organization`, `Project`, `Environment`, `Repository`, `ManagedBy`, `Owner`, `Component`.
- Work through [docs/migration.md](docs/migration.md) to retire the legacy resources.

## License

[MIT](LICENSE)
