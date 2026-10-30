# Terraform state bucket shared by every jwall-engineering stack.
#
# Key convention: project=<project>/env=<env>/<project>-<env>.tfstate
# Locking uses the S3 native lockfile (<key>.tflock); there is no DynamoDB.
# The module hard-codes prevent_destroy.
#
# The bucket policy is hardened: each state prefix is reachable only by the CI
# roles listed for it, read-only roles may write only .tflock objects, and
# only the break-glass admins may touch anything else or change the bucket's
# configuration. Every statement is a deny, so this bounds other principals in
# the account (including ones with s3:* on *) without granting anything.

locals {
  state_bucket_name = "jwall-engineering-tfstate"

  # This stack's own state, used by the CI role policies. Must match
  # backend/prod.hcl, which cannot reference variables.
  state_key = "project=infra/env=prod/infra-prod.tfstate"

  # Break-glass principals exempt from the hardened bucket policies on the
  # state bucket and every site bucket. The apply role must be here: it
  # applies those policies. The root user is here so the owner can browse the
  # buckets in the console. Being exempt grants nothing by itself.
  bucket_admin_principal_arns = [
    "arn:aws:iam::${local.account_id}:root",
    "arn:aws:iam::${local.account_id}:user/prog-strength-admin",
    module.infra_ci_apply.role_arn,
  ]

  # State prefix -> who may read and write it. The infra entry is this stack's
  # exact key (which also matches its .tflock); each project gets its prefix.
  state_prefix_access = merge(
    {
      (local.state_key) = {
        read  = [module.infra_ci_plan.role_arn]
        write = [module.infra_ci_apply.role_arn]
      }
    },
    {
      for key, project in local.projects : "project=${key}/" => {
        read  = [module.site_ci_plan[key].role_arn]
        write = [module.site_ci_apply[key].role_arn]
      }
    },
    {
      for key, bot in local.bots : "project=${key}/" => {
        read  = [module.bot_ci_plan[key].role_arn]
        write = [module.bot_ci_apply[key].role_arn]
      }
    },
  )
}

module "state_bucket" {
  source  = "jwallace145/jwall-engineering/aws//modules/tfstate-bucket"
  version = "0.16.0"

  name = local.state_bucket_name

  admin_principal_arns = local.bucket_admin_principal_arns
  prefix_access        = local.state_prefix_access
}
