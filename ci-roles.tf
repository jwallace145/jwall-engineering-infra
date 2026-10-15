# This repository's own CI roles, assumed by GitHub Actions through OIDC.
#
#   jwall-engineering-infra-ci-plan   trusted for pull_request      tfstate/read, foundation/read
#   jwall-engineering-infra-ci-apply  trusted for environment:prod  tfstate/write, foundation/read, foundation/manage
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
  }
}

module "infra_ci_plan" {
  source  = "jwallace145/jwall-engineering/aws//modules/ci-plan-role"
  version = "0.6.0"

  project           = local.infra_ci.project
  name_root         = local.context.organization
  repository        = local.context.repository
  owner_id          = local.github.owner_id
  repository_id     = local.github.repository_id
  oidc_provider_arn = data.aws_iam_openid_connect_provider.github.arn

  policies = ["tfstate/read", "foundation/read"]

  policy_inputs = {
    tfstate    = local.infra_ci.tfstate
    foundation = local.infra_ci.foundation
  }
}

module "infra_ci_apply" {
  source  = "jwallace145/jwall-engineering/aws//modules/ci-apply-role"
  version = "0.6.0"

  project           = local.infra_ci.project
  name_root         = local.context.organization
  repository        = local.context.repository
  owner_id          = local.github.owner_id
  repository_id     = local.github.repository_id
  oidc_provider_arn = data.aws_iam_openid_connect_provider.github.arn
  environment       = "prod"

  policies = ["tfstate/write", "foundation/read", "foundation/manage"]

  policy_inputs = {
    tfstate    = local.infra_ci.tfstate
    foundation = local.infra_ci.foundation
  }
}

# 0.6.0 replaced infra-ci-roles with one module per role. The roles keep their
# names (and ARNs); their inline policies and the ReadOnlyAccess attachment are
# destroyed and replaced by the managed policies above. Remove these blocks
# once the move has been applied.
moved {
  from = module.infra_ci_roles.module.plan
  to   = module.infra_ci_plan.module.role
}

moved {
  from = module.infra_ci_roles.module.apply
  to   = module.infra_ci_apply.module.role
}
