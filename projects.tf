# The single registry of jwall-engineering sites. Adding a site means adding a
# line here; each entry gets an S3 bucket and three CI roles (plan, preview,
# apply) scoped to its repository and domain.
#
# Repository names were confirmed with the owner on 2026-10-14.

locals {
  projects = {
    main-site             = { repository = "jwallace145/site", domain = "jwall.engineering" }
    cs-survival-guide     = { repository = "jwallace145/cs-survival-guide", domain = "survival-guide.jwall.engineering" }
    guitar-knowledge-base = { repository = "jwallace145/guitar-knowledge-base", domain = "guitar.jwall.engineering" }
  }

  # Record names each project's CI roles may manage in the zone: the site
  # itself, its wildcard preview hostnames, and the ACM validation CNAMEs for
  # those names.
  project_record_names = {
    for key, project in local.projects : key => [
      project.domain,
      "*.preview.${project.domain}",
    ]
  }
}

# TODO(terraform-library v0.1.0): the per-project resources are consumed from
# the library and cannot be written until it is released and its module
# interfaces are known. The intended shape is:
#
#   module "site_bucket" {
#     for_each = local.projects
#     source   = "git::https://github.com/jwallace145/terraform-library.git//modules/site-bucket?ref=v0.1.0"
#     ...
#   }
#
#   module "site_ci_roles" {
#     for_each = local.projects
#     source   = "git::https://github.com/jwallace145/terraform-library.git//modules/site-ci-roles?ref=v0.1.0"
#     repository        = each.value.repository
#     oidc_provider_arn = aws_iam_openid_connect_provider.github.arn
#     bucket            = module.site_bucket[each.key]...
#     hosted_zone_id    = aws_route53_zone.this.zone_id
#     record_names      = local.project_record_names[each.key]  # plus ACM validation records
#     ...
#   }
#
# Constraints the library must satisfy for this stack's apply role (see
# ci-roles.tf): bucket names start with "jwall-engineering-" or "<project>-",
# and role/policy names start with "jwall-engineering-".
