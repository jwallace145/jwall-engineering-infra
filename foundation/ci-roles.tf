# This repository's own CI roles, assumed by GitHub Actions through OIDC.
#
#   jwall-engineering-infra-ci-plan   trusted for pull_request      tfstate/read, foundation/read, databricks/read
#   jwall-engineering-infra-ci-apply  trusted for environment:prod  tfstate/write, foundation/read, foundation/manage, ecr/push,
#                                                                   databricks/read
#
# databricks/read lets both roles read the Databricks client secret, which
# the foundation's Databricks resources (databricks.tf) authenticate with.
# It does not wait for databricks.enabled: the plan of the pull request that
# flips it to true must already be able to read the secret.
#
# Each role gets its permissions as managed policies from the library's
# ci-policies catalog, built for this stack. The plan role only accepts read
# policies. The apply role is the highest-privilege identity in the setup:
# foundation/manage restricts its IAM permissions to roles and policies named
# jwall-engineering-* and allow-lists the managed policies it may attach, so it
# cannot attach an AWS-managed policy such as AdministratorAccess. It can still
# edit jwall-engineering-* roles and policies, itself included, so the prod
# environment must stay protected. The full justification is in the
# ci-policies README; a summary is in README.md.

locals {
  infra_ci = {
    # Short role-name segment (jwall-engineering-infra-ci-plan/apply).
    # Deliberately not local.context.project, which is the Project tag value;
    # changing this renames and replaces the roles.
    project = "infra"

    tfstate = {
      bucket_arn = module.state_bucket.arn
      key        = local.state_key
    }

    foundation = {
      state_bucket_arn = module.state_bucket.arn
      hosted_zone_arn  = module.zone.arn
    }

    # The sites CLI image this repo publishes on release.
    ecr = {
      repository_arn = local.sites_image_repository_arn
    }

    # databricks/read: the secret, and this stack's events under project=infra/
    # (the literal is local.infra_ci.project, which cannot be referenced here).
    databricks = {
      secret_parameter_arn = local.databricks_secret_parameter_arn
      events_object_arn    = "${local.databricks_events_arn_prefix}project=infra/*"
    }
  }
}

module "infra_ci_plan" {
  source  = "jwallace145/jwall-engineering/aws//modules/ci-plan-role"
  version = "0.18.0"

  project           = local.infra_ci.project
  name_root         = local.context.organization
  repository        = local.context.repository
  owner_id          = local.github.owner_id
  repository_id     = local.github.repository_id
  oidc_provider_arn = data.aws_iam_openid_connect_provider.github.arn

  policies = ["tfstate/read", "foundation/read", "databricks/read"]

  policy_inputs = {
    tfstate    = local.infra_ci.tfstate
    foundation = local.infra_ci.foundation
    databricks = local.infra_ci.databricks
  }
}

module "infra_ci_apply" {
  source  = "jwallace145/jwall-engineering/aws//modules/ci-apply-role"
  version = "0.18.0"

  project           = local.infra_ci.project
  name_root         = local.context.organization
  repository        = local.context.repository
  owner_id          = local.github.owner_id
  repository_id     = local.github.repository_id
  oidc_provider_arn = data.aws_iam_openid_connect_provider.github.arn
  environment       = "prod"

  policies = ["tfstate/write", "foundation/read", "foundation/manage", "ecr/push", "databricks/read"]

  policy_inputs = {
    tfstate    = local.infra_ci.tfstate
    foundation = local.infra_ci.foundation
    ecr        = local.infra_ci.ecr
    databricks = local.infra_ci.databricks
  }
}
