# Migration: legacy resources to retire

This stack adopts the `jwall.engineering` hosted zone that `jwallace145/site` created first and introduces a shared state bucket and per-project buckets and roles. The main site's current bucket and every existing per-project deploy role are **left untouched** by this repo. The owner retires them once each project has moved to the resources this stack provides.

Inventory taken on 2026-10-14 from `jwallace145/site` (`infra/`) and its repository variables. AWS account `650503560686`, region `us-east-1`. Resource names follow that repo's `<name_prefix>-<domain>-<resource>` convention with `name_prefix = jwall-engineering-site`.

## Imported here (ownership moves, nothing is recreated)

| Resource             | Identifier                                                                    | Note                                                                   |
| -------------------- | ----------------------------------------------------------------------------- | ---------------------------------------------------------------------- |
| Route 53 hosted zone | `jwall.engineering` (`module.dns[0].aws_route53_zone.this` in the site state) | Import changes tags and the zone comment. See "Hosted zone hand-over". |

## Shared, not imported: the GitHub OIDC provider

`arn:aws:iam::650503560686:oidc-provider/token.actions.githubusercontent.com` is the account's only GitHub OIDC provider (IAM allows one per URL) and is owned by the prog-strength infra stack (tagged `ManagedBy = prog-strength-infra`). This stack and the site repo reference it with data sources. Moving ownership here is a later task; the steps are in the TODO in `oidc.tf`.

## Legacy resources to retire

Nothing below is managed by this repo. Retire each item only after its replacement is live and verified.

### Retired

Retired on 2026-10-15 after the main site deployed from this stack's resources: the
site repo's old stack (`jwall-engineering-site-*` buckets, CloudFront distributions, ACM
certificates, deploy, plan and preview roles, budget and the `www` record) was
destroyed, and the `production` and `preview` GitHub environments and the old
`AWS_ROLE_ARN`, `S3_BUCKET` and `AWS_REGION` variables were deleted. The old state bucket
`jwall-engineering-site-tfstate` was deleted; state now lives in `jwall-engineering-tfstate`, key
`project=main-site/env=prod/main-site-prod.tfstate`.

### CS Survival Guide and Guitar Knowledge Base

Both are served by CloudFront from their own project repos. The Pages CNAMEs are gone and
`letsencrypt.org` is no longer in the CAA issuer list. The only leftover is the
`_github-pages-challenge-jwallace145.jwall.engineering` TXT domain-verification record,
which no stack manages and can be deleted.

## Hosted zone hand-over

After bootstrap, the zone is in two Terraform states: this stack's and the site repo's (`module.dns[0].aws_route53_zone.this`). Until the site repo is updated, an apply there would try to reset the zone's comment and tags. To hand over cleanly, in `jwallace145/site`:

1. Remove the zone (and nothing else) from the site's state:

   ```sh
   terraform state rm 'module.dns[0].aws_route53_zone.this'
   ```

2. Point the site at the existing zone instead of creating one. In `infra/environments/prod/terraform.tfvars` set `dns.zone_name = null` and `dns.hosted_zone_id = "<zone id from this stack's outputs>"`, keeping `dns.records` until those records move to their project repos.

3. Plan in the site repo and confirm it shows no changes to the zone.

## Order of operations

1. Bootstrap this stack (README). Imports the zone; creates the state bucket, this repo's CI roles, the three project buckets and the nine project CI roles.
2. Hand over the hosted zone (above).
3. Move each project repo to its new bucket and roles, verify a deploy, then retire that project's legacy rows above.
4. Migrate the site repo's state into the shared bucket (key `project=main-site/env=prod/main-site-prod.tfstate`), then retire `jwall-engineering-site-tfstate`.
