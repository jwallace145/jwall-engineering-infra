provider "aws" {
  region = local.region

  # Every resource this stack creates carries the organisation-wide tag set.
  # Per-resource `tags` add `Component` and `Name` on top.
  default_tags {
    tags = module.context.tags
  }
}

# The Databricks account API, for the metastore and audit log delivery
# (databricks.tf). Credentials come from the environment only
# (DATABRICKS_ACCOUNT_ID, DATABRICKS_CLIENT_ID, DATABRICKS_CLIENT_SECRET, set
# by .github/actions/databricks-credentials), never from variables, so the
# secret is in no plan or state. While config.yaml databricks.enabled is false
# nothing uses this provider and it makes no call.
provider "databricks" {
  alias     = "account"
  host      = "https://accounts.cloud.databricks.com"
  auth_type = "oauth-m2m"
}
