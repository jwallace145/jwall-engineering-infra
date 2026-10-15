# The single registry of jwall-engineering sites. Adding a site means adding a
# line here; each entry gets an S3 bucket (jwall-engineering-<key>) and three
# CI roles (jwall-engineering-<key>-ci-{plan,preview,apply}) scoped to its
# repository, bucket, state prefix and DNS names.
#
# Modules come from terraform-library, pinned to a release tag.

locals {
  projects = {
    main-site             = { repository = "jwallace145/site", domain = "jwall.engineering" }
    cs-survival-guide     = { repository = "jwallace145/cs-survival-guide", domain = "survival-guide.jwall.engineering" }
    guitar-knowledge-base = { repository = "jwallace145/guitar-knowledge-base", domain = "guitar.jwall.engineering" }
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
  source   = "git::https://github.com/jwallace145/terraform-library.git//modules/site-bucket?ref=v0.1.0"

  project = each.key
}

module "site_ci_roles" {
  for_each = local.projects
  source   = "git::https://github.com/jwallace145/terraform-library.git//modules/site-ci-roles?ref=v0.1.0"

  project           = each.key
  repository        = each.value.repository
  bucket_arn        = module.site_bucket[each.key].arn
  state_bucket_arn  = aws_s3_bucket.tfstate.arn
  oidc_provider_arn = aws_iam_openid_connect_provider.github.arn
  zone_id           = aws_route53_zone.this.zone_id
  record_names      = local.project_record_names[each.key]
}
