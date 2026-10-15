# This repository's own CI roles, assumed by GitHub Actions through OIDC.
#
#   jwall-engineering-infra-ci-plan   trusted for pull_request      read-only + state lock
#   jwall-engineering-infra-ci-apply  trusted for environment:prod  manages everything in this stack
#
# The apply role is the highest-privilege identity in the setup. The module
# restricts its IAM permissions to roles and policies named
# jwall-engineering-* and allow-lists the managed policies it may attach, so a
# compromise of the prod environment cannot mint an administrator. The full
# justification is in the module README; a summary is in README.md.

module "infra_ci_roles" {
  source  = "jwallace145/jwall-engineering/aws//modules/infra-ci-roles"
  version = "0.4.0"

  # Short role-name segment (jwall-engineering-infra-ci-plan/apply). Deliberately
  # not local.context.project, which is the Project tag value; changing this
  # renames and replaces the roles.
  project           = "infra"
  name_root         = local.context.organization
  repository        = local.context.repository
  owner_id          = local.github.owner_id
  repository_id     = local.github.repository_id
  oidc_provider_arn = data.aws_iam_openid_connect_provider.github.arn
  apply_environment = "prod"

  state_bucket_arn = module.state_bucket.arn
  state_key        = local.state_key
  hosted_zone_arn  = module.zone.arn
}
