# One site: the production distribution for domain_name served from
# <bucket>/prod, optionally a dev distribution from <bucket>/dev, and the
# pr-<n>.preview.<domain_name> distribution served from <bucket>/previews/pr-<n>.
#
# The bucket and the CI roles are owned by the foundation stack in this
# repository and only referenced here by name.

locals {
  bucket_name = "${var.tags["Organization"]}-${var.project}"
}

module "site" {
  source  = "jwallace145/jwall-engineering/aws"
  version = "0.14.0"

  bucket_name                 = local.bucket_name
  bucket_regional_domain_name = "${local.bucket_name}.s3.us-east-1.amazonaws.com"
  domain_name                 = var.domain_name
  zone_id                     = var.zone_id
  dev_domain_name             = var.dev_domain_name
  enable_preview              = var.enable_preview
}
