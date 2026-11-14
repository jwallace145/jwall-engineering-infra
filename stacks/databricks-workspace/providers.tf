provider "aws" {
  region = "us-east-1"

  # Every resource carries the project's tags. The Project tag is what the
  # apply role's IAM policies key on, so this block is load-bearing.
  default_tags {
    tags = var.tags
  }
}

# The workspace-level API of the workspace the account stack created, as the
# same service principal (a member of the workspace's admin group). The CLI
# passes DATABRICKS_CLIENT_ID and DATABRICKS_CLIENT_SECRET in Terraform's
# environment only; DATABRICKS_HOST is not used, the host is this variable.
provider "databricks" {
  alias     = "workspace"
  host      = var.workspace_url
  auth_type = "oauth-m2m"
}
