# The private ECR repository that holds the jwall-engineering-sites CLI image.
# Every site's CI roles pull it (ecr/pull); this repo's apply role pushes a
# tagged image on each release (ecr/push). Name and lifecycle come from
# config.yaml, the tool's configuration, so the CLI and the repository that
# holds it agree.

locals {
  tool_config = yamldecode(file("${path.module}/config.yaml"))
}

module "sites_image" {
  source  = "jwallace145/jwall-engineering/aws//modules/ecr-repository"
  version = "0.8.1"

  name                       = local.tool_config.ecr.repository
  keep_release_images        = local.tool_config.ecr.keep_release_images
  expire_untagged_after_days = local.tool_config.ecr.expire_untagged_after_days
}
