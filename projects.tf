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
  version  = "0.6.0"

  project = each.key
}

# Each project's CI roles get their permissions as managed policies from the
# library's ci-policies catalog. Plan only accepts read policies and preview
# only static-site/previews, because both are assumable from any pull request.
locals {
  project_ci = {
    for key, project in local.projects : key => {
      tfstate = {
        bucket_arn = module.state_bucket.arn
        prefix     = "project=${key}/"
      }
      static_site = {
        bucket_arn   = module.site_bucket[key].arn
        zone_id      = module.zone.zone_id
        record_names = local.project_record_names[key]
      }
    }
  }
}

module "site_ci_plan" {
  for_each = local.projects
  source   = "jwallace145/jwall-engineering/aws//modules/ci-plan-role"
  version  = "0.6.0"

  project           = each.key
  repository        = each.value.repository
  owner_id          = local.github_owner_id
  repository_id     = each.value.repository_id
  oidc_provider_arn = data.aws_iam_openid_connect_provider.github.arn

  policies = ["tfstate/read", "static-site/read"]

  policy_inputs = {
    tfstate     = local.project_ci[each.key].tfstate
    static_site = local.project_ci[each.key].static_site
  }
}

module "site_ci_preview" {
  for_each = local.projects
  source   = "jwallace145/jwall-engineering/aws//modules/ci-preview-role"
  version  = "0.6.0"

  project           = each.key
  repository        = each.value.repository
  owner_id          = local.github_owner_id
  repository_id     = each.value.repository_id
  oidc_provider_arn = data.aws_iam_openid_connect_provider.github.arn

  policies = ["static-site/previews"]

  policy_inputs = {
    static_site = local.project_ci[each.key].static_site
  }
}

module "site_ci_apply" {
  for_each = local.projects
  source   = "jwallace145/jwall-engineering/aws//modules/ci-apply-role"
  version  = "0.6.0"

  project           = each.key
  repository        = each.value.repository
  owner_id          = local.github_owner_id
  repository_id     = each.value.repository_id
  oidc_provider_arn = data.aws_iam_openid_connect_provider.github.arn
  environment       = "prod"

  policies = ["tfstate/write", "static-site/read", "static-site/deploy"]

  policy_inputs = {
    tfstate     = local.project_ci[each.key].tfstate
    static_site = local.project_ci[each.key].static_site
  }
}

# 0.6.0 replaced site-ci-roles with one module per role. The roles keep their
# names (and ARNs); their inline policies are destroyed and replaced by the
# managed policies above. moved blocks cannot use for_each, so there is one
# per project and role. Remove these blocks once the move has been applied.
moved {
  from = module.site_ci_roles["main-site"].module.role["plan"]
  to   = module.site_ci_plan["main-site"].module.role
}

moved {
  from = module.site_ci_roles["main-site"].module.role["preview"]
  to   = module.site_ci_preview["main-site"].module.role
}

moved {
  from = module.site_ci_roles["main-site"].module.role["apply"]
  to   = module.site_ci_apply["main-site"].module.role
}

moved {
  from = module.site_ci_roles["cs-survival-guide"].module.role["plan"]
  to   = module.site_ci_plan["cs-survival-guide"].module.role
}

moved {
  from = module.site_ci_roles["cs-survival-guide"].module.role["preview"]
  to   = module.site_ci_preview["cs-survival-guide"].module.role
}

moved {
  from = module.site_ci_roles["cs-survival-guide"].module.role["apply"]
  to   = module.site_ci_apply["cs-survival-guide"].module.role
}

moved {
  from = module.site_ci_roles["guitar-knowledge-base"].module.role["plan"]
  to   = module.site_ci_plan["guitar-knowledge-base"].module.role
}

moved {
  from = module.site_ci_roles["guitar-knowledge-base"].module.role["preview"]
  to   = module.site_ci_preview["guitar-knowledge-base"].module.role
}

moved {
  from = module.site_ci_roles["guitar-knowledge-base"].module.role["apply"]
  to   = module.site_ci_apply["guitar-knowledge-base"].module.role
}
