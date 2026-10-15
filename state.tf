# Terraform state bucket shared by every jwall-engineering stack.
#
# Key convention: project=<project>/env=<env>/<project>-<env>.tfstate
# Locking uses the S3 native lockfile (<key>.tflock); there is no DynamoDB.
# The module hard-codes prevent_destroy.

locals {
  state_bucket_name = "jwall-engineering-tfstate"

  # This stack's own state, used by the CI role policies. Must match
  # backend/prod.hcl, which cannot reference variables.
  state_key = "project=infra/env=prod/infra-prod.tfstate"
}

module "state_bucket" {
  source  = "jwallace145/jwall-engineering/aws//modules/tfstate-bucket"
  version = "0.5.0"

  name = local.state_bucket_name
}
