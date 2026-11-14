provider "aws" {
  region = "us-east-1"

  # Every resource carries the project's tags. The Project tag is what the
  # apply role's IAM policies key on, so this block is load-bearing.
  default_tags {
    tags = var.tags
  }
}

# The account-level API: workspaces, networks, credentials, groups and the
# metastore assignment. The CLI passes the service principal's credentials
# (DATABRICKS_ACCOUNT_ID, DATABRICKS_CLIENT_ID, DATABRICKS_CLIENT_SECRET) in
# Terraform's environment only, never as variables, so the secret is in no
# tfvars file, plan or state.
provider "databricks" {
  alias     = "account"
  host      = "https://accounts.cloud.databricks.com"
  auth_type = "oauth-m2m"
}
