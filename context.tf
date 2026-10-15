# Identity of this stack: who owns it, which repo manages it, and the tag set
# that default_tags stamps on everything.
#
# TODO(terraform-library v0.1.0): replace `local.tags` with the library's
# context module once it is released, so every jwall-engineering stack derives
# its tags from the same source:
#
#   module "context" {
#     source      = "git::https://github.com/jwallace145/terraform-library.git//modules/context?ref=v0.1.0"
#     project     = "infra"
#     environment = "prod"
#     repository  = "jwallace145/jwall-engineering-infra"
#   }
#
#   provider "aws" { default_tags { tags = module.context.tags } }
#
# The tag keys below are the cost-allocation tags the owner activates in
# Billing; keep them in sync with the library.
locals {
  region = "us-east-1"

  context = {
    organization = "jwall-engineering"
    project      = "infra"
    environment  = "prod"
    repository   = "jwallace145/jwall-engineering-infra"
    owner        = "jwallace145"
  }

  tags = {
    Organization = local.context.organization
    Project      = local.context.project
    Environment  = local.context.environment
    Repository   = local.context.repository
    ManagedBy    = "terraform"
    Owner        = local.context.owner
  }

  account_id = data.aws_caller_identity.current.account_id
  partition  = data.aws_partition.current.partition

  # Resource name prefix for everything owned by this stack.
  name_prefix = "${local.context.organization}-${local.context.project}"
}
