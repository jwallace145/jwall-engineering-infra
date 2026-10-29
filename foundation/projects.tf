# The single registry of jwall-engineering sites. Adding a site means adding a
# line here; each entry gets an S3 bucket (jwall-engineering-<key>) and three
# CI roles (jwall-engineering-<key>-ci-{plan,preview,apply}) scoped to its
# repository, bucket, state prefix and DNS names.
#
# Modules come from the jwallace145/jwall-engineering/aws registry module,
# pinned to an exact version in every block (module versions are not recorded
# in .terraform.lock.hcl, so a range would not be reproducible). Dependabot
# opens bump PRs; keep every version pin in this repo identical.

locals {
  # repository_id is the numeric GitHub ID (gh api repos/<owner>/<repo> --jq .id).
  # Together with github_owner_id it lets the CI roles trust GitHub's immutable
  # OIDC subject form, which every repository here issues.
  github_owner_id = "35950238"

  projects = {
    main-site             = { repository = "jwallace145/site", repository_id = "1413965497", domain = "jwall.engineering" }
    cs-survival-guide     = { repository = "jwallace145/cs-survival-guide", repository_id = "1402607832", domain = "survival-guide.jwall.engineering" }
    guitar-knowledge-base = { repository = "jwallace145/guitar-knowledge-base", repository_id = "1418340665", domain = "guitar.jwall.engineering" }
  }

  # Record names each project's CI roles may manage in the zone: the site
  # itself and its wildcard preview hostnames. The module adds the ACM
  # validation CNAMEs for these names automatically.
  project_record_names = {
    for key, project in local.projects : key => [
      project.domain,
      "*.preview.${project.domain}",
    ]
  }
}

module "site_bucket" {
  for_each = local.projects
  source   = "jwallace145/jwall-engineering/aws//modules/site-bucket"
  version  = "0.12.0"

  project = each.key

  # Hardened bucket policy: only the project's apply role writes prod/ and
  # dev/, only its preview role writes previews/, and only the break-glass
  # admins (state.tf) write anything else or change the bucket's
  # configuration. Reads are unrestricted; the content is public through
  # CloudFront.
  admin_principal_arns = local.bucket_admin_principal_arns
  prefix_writers = {
    "prod/"     = [module.site_ci_apply[each.key].role_arn]
    "dev/"      = [module.site_ci_apply[each.key].role_arn]
    "previews/" = [module.site_ci_preview[each.key].role_arn]
  }
}

# Each project's CI roles get their permissions as managed policies from the
# library's ci-policies catalog. Plan only accepts read policies and preview
# only static-site/previews, because both are assumable from any pull request.
# Every role also pulls the jwall-engineering-sites CLI image (ecr/pull).
locals {
  project_ci = {
    for key, project in local.projects : key => {
      tfstate = {
        bucket_arn = module.state_bucket.arn
        prefix     = "project=${key}/"
      }
      static_site = {
        # Built from the bucket's fixed name, not module.site_bucket[key].arn:
        # each site bucket's policy names these roles, so reading the bucket
        # module here would be a dependency cycle.
        bucket_arn   = "arn:aws:s3:::jwall-engineering-${key}"
        zone_id      = module.zone.zone_id
        record_names = local.project_record_names[key]
      }
      ecr = {
        repository_arn = local.sites_image_repository_arn
      }
    }
  }
}

module "site_ci_plan" {
  for_each = local.projects
  source   = "jwallace145/jwall-engineering/aws//modules/ci-plan-role"
  version  = "0.12.0"

  project           = each.key
  repository        = each.value.repository
  owner_id          = local.github_owner_id
  repository_id     = each.value.repository_id
  oidc_provider_arn = data.aws_iam_openid_connect_provider.github.arn

  policies = ["tfstate/read", "static-site/read", "ecr/pull"]

  policy_inputs = {
    tfstate     = local.project_ci[each.key].tfstate
    static_site = local.project_ci[each.key].static_site
    ecr         = local.project_ci[each.key].ecr
  }
}

module "site_ci_preview" {
  for_each = local.projects
  source   = "jwallace145/jwall-engineering/aws//modules/ci-preview-role"
  version  = "0.12.0"

  project           = each.key
  repository        = each.value.repository
  owner_id          = local.github_owner_id
  repository_id     = each.value.repository_id
  oidc_provider_arn = data.aws_iam_openid_connect_provider.github.arn

  policies = ["static-site/previews", "ecr/pull"]

  policy_inputs = {
    static_site = local.project_ci[each.key].static_site
    ecr         = local.project_ci[each.key].ecr
  }
}

module "site_ci_apply" {
  for_each = local.projects
  source   = "jwallace145/jwall-engineering/aws//modules/ci-apply-role"
  version  = "0.12.0"

  project           = each.key
  repository        = each.value.repository
  owner_id          = local.github_owner_id
  repository_id     = each.value.repository_id
  oidc_provider_arn = data.aws_iam_openid_connect_provider.github.arn
  environment       = "prod"

  policies = ["tfstate/write", "static-site/read", "static-site/deploy", "ecr/pull"]

  policy_inputs = {
    tfstate     = local.project_ci[each.key].tfstate
    static_site = local.project_ci[each.key].static_site
    ecr         = local.project_ci[each.key].ecr
  }
}

# local.project_ci builds each site bucket ARN from the bucket's name to avoid
# a dependency cycle. Flag it if that ever drifts from the module's output.
check "site_bucket_arns_match" {
  assert {
    condition = alltrue([
      for key, project in local.projects :
      module.site_bucket[key].arn == local.project_ci[key].static_site.bucket_arn
    ])
    error_message = "local.project_ci builds a site bucket ARN that differs from module.site_bucket's arn; the CI role policies would point at the wrong bucket."
  }
}
