# The private ECR repository that holds the jwall-engineering-sites CLI image.
# Every site's CI roles pull it (ecr/pull); this repo's apply role pushes a
# tagged image on each release (ecr/push). Name and lifecycle come from
# config.yaml, the tool's configuration, so the CLI and the repository that
# holds it agree.

data "aws_caller_identity" "current" {}

locals {
  # config.yaml lives at the repository root, next to the CLI that reads it.
  tool_config = yamldecode(file("${path.module}/../config.yaml"))

  # Built from the configured name rather than module.sites_image.arn so the
  # roles' ECR policies do not depend on the repository. That lets the
  # repository depend on the apply role below, which orders its creation after
  # the foundation/manage update that grants ecr:CreateRepository on a fresh
  # account. A check below flags any drift from the module's own ARN.
  sites_image_repository_arn = "arn:aws:ecr:${local.region}:${data.aws_caller_identity.current.account_id}:repository/${local.tool_config.ecr.repository}"
}

module "sites_image" {
  source  = "jwallace145/jwall-engineering/aws//modules/ecr-repository"
  version = "0.11.0"

  name                       = local.tool_config.ecr.repository
  keep_release_images        = local.tool_config.ecr.keep_release_images
  expire_untagged_after_days = local.tool_config.ecr.expire_untagged_after_days

  depends_on = [module.infra_ci_apply]
}

check "sites_image_repository_arn_matches" {
  assert {
    condition     = module.sites_image.arn == local.sites_image_repository_arn
    error_message = "local.sites_image_repository_arn differs from module.sites_image's arn; the roles' ECR policies would point at the wrong repository."
  }
}

# config.yaml names the account the workflows build role ARNs for; a plan
# against any other account is a mistake worth flagging.
check "configured_account_matches" {
  assert {
    condition     = data.aws_caller_identity.current.account_id == local.account_id
    error_message = "config.yaml aws.account_id (${local.account_id}) is not the account these credentials belong to (${data.aws_caller_identity.current.account_id})."
  }
}
