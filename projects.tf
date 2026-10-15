# The single registry of jwall-engineering sites. Adding a site means adding a
# line here; each entry gets an S3 bucket (jwall-engineering-<key>) and three
# CI roles (jwall-engineering-<key>-ci-{plan,preview,apply}) scoped to its
# repository, bucket, state prefix and DNS names.
#
# Modules come from the jwallace145/jwall-engineering/aws registry module,
# pinned to an exact version in every block (module versions are not recorded
# in .terraform.lock.hcl, so a range would not be reproducible). Dependabot
# opens bump PRs; keep the three pins in context.tf and here identical.

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
  version  = "0.3.0"

  project = each.key
}

module "site_ci_roles" {
  for_each = local.projects
  source   = "jwallace145/jwall-engineering/aws//modules/site-ci-roles"
  version  = "0.3.0"

  project           = each.key
  repository        = each.value.repository
  owner_id          = local.github_owner_id
  repository_id     = each.value.repository_id
  bucket_arn        = module.site_bucket[each.key].arn
  state_bucket_arn  = aws_s3_bucket.tfstate.arn
  oidc_provider_arn = data.aws_iam_openid_connect_provider.github.arn
  zone_id           = aws_route53_zone.this.zone_id
  record_names      = local.project_record_names[each.key]
}
