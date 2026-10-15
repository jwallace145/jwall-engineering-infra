# Migration: legacy resources to retire

This stack adopts the account-wide pieces that `jwallace145/site` created first (the GitHub OIDC provider and the `jwall.engineering` hosted zone) and introduces a shared state bucket and per-project buckets and roles. The main site's current bucket and every existing per-project deploy role are **left untouched** by this repo. The owner retires them once each project has moved to the resources this stack provides.

Inventory taken on 2026-10-14 from `jwallace145/site` (`infra/`) and its repository variables. AWS account `650503560686`, region `us-east-1`. Resource names follow that repo's `<name_prefix>-<domain>-<resource>` convention with `name_prefix = jwall-engineering-site`.

## Imported here (ownership moves, nothing is recreated)

| Resource | Identifier | Note |
| --- | --- | --- |
| GitHub OIDC provider | `arn:aws:iam::650503560686:oidc-provider/token.actions.githubusercontent.com` | Was created outside the site's Terraform (`github.create_oidc_provider = false`). Import adds tags only. |
| Route 53 hosted zone | `jwall.engineering` (`module.dns[0].aws_route53_zone.this` in the site state) | Import changes tags and the zone comment. See "Hosted zone hand-over". |

## Legacy resources to retire

Nothing below is managed by this repo. Retire each item only after its replacement is live and verified.

### State

| Resource | Replacement | Retire when |
| --- | --- | --- |
| S3 bucket `jwall-engineering-site-tfstate` (key `environment=prod/terraform.tfstate`) | `jwall-engineering-tfstate`, key `project=main-site/env=prod/main-site-prod.tfstate` | The site repo's state has been migrated to the shared bucket |

### Main site (`jwallace145/site`)

| Resource | Type | Replacement | Retire when |
| --- | --- | --- | --- |
| `jwall-engineering-site-web-bucket` | S3 bucket (CloudFront origin) | `main-site` site bucket from this stack | CloudFront origin points at the new bucket and a release has deployed to it |
| `jwall-engineering-site-preview-bucket` | S3 bucket (PR previews) | `main-site` site bucket, preview prefix | Preview workflow deploys to the new bucket |
| `jwall-engineering-site-deploy-github-role` | IAM role, trusted for `environment:production` | `main-site` apply role | `release.yml` uses the new role ARN |
| `jwall-engineering-site-deploy-github-plan-role` | IAM role, trusted for `pull_request` | `main-site` plan role | `ci.yml` uses the new role ARN (repository variable `AWS_PLAN_ROLE_ARN`) |
| `jwall-engineering-site-preview-github-role` | IAM role, trusted for `environment:preview` | `main-site` preview role | `preview.yml` uses the new role ARN |
| `jwall-engineering-site-deploy-github-policy`, `-plan-policy` | Inline role policies | Policies attached by the `site-ci-roles` module | Deleted with their roles |

Repository variables in `jwallace145/site` that change: `AWS_PLAN_ROLE_ARN` (currently `arn:aws:iam::650503560686:role/jwall-engineering-site-deploy-github-plan-role`), `AWS_ROLE_ARN`, and `S3_BUCKET`.

### CS Survival Guide (`jwallace145/cs-survival-guide`) and Guitar Knowledge Base (`jwallace145/guitar-knowledge-base`)

Both are served by GitHub Pages today. They have no AWS deploy roles or buckets to retire. The records that point them at Pages live in the site repo's Terraform:

| Record | Type | Value | Retire when |
| --- | --- | --- | --- |
| `survival-guide.jwall.engineering` | CNAME | `jwallace145.github.io.` | The project repo owns the record (an alias to its CloudFront distribution) |
| `guitar.jwall.engineering` | CNAME | `jwallace145.github.io.` | Same |
| `_github-pages-challenge-jwallace145.jwall.engineering` | TXT | GitHub Pages domain verification | Both sites have left GitHub Pages |

While either site is on GitHub Pages, keep `letsencrypt.org` in the CAA issuer list in `dns.tf`. Remove it after both have moved to CloudFront + ACM.

## Hosted zone hand-over

After bootstrap, the zone is in two Terraform states: this stack's and the site repo's (`module.dns[0].aws_route53_zone.this`). Until the site repo is updated, an apply there would try to reset the zone's comment and tags. To hand over cleanly, in `jwallace145/site`:

1. Remove the zone (and nothing else) from the site's state:

   ```sh
   terraform state rm 'module.dns[0].aws_route53_zone.this'
   ```

2. Point the site at the existing zone instead of creating one. In `infra/environments/prod/terraform.tfvars` set `dns.zone_name = null` and `dns.hosted_zone_id = "<zone id from this stack's outputs>"`, keeping `dns.records` until those records move to their project repos.

3. Plan in the site repo and confirm it shows no changes to the zone.

## Order of operations

1. Bootstrap this stack (README). Imports the OIDC provider and zone; creates the state bucket, this repo's CI roles, the three project buckets and the nine project CI roles.
2. Hand over the hosted zone (above).
3. Move each project repo to its new bucket and roles, verify a deploy, then retire that project's legacy rows above.
4. Migrate the site repo's state into the shared bucket (key `project=main-site/env=prod/main-site-prod.tfstate`), then retire `jwall-engineering-site-tfstate`.
