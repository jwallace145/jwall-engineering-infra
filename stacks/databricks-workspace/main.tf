# The workspace stack of one Databricks project, applied after the account
# stack: its inputs include the account stack's outputs. The project's catalog
# (bucket, Unity Catalog role, storage credential, external location, grants)
# and the workspace's settings (the platform's compute policies, entitlements,
# token lifetime and, in the governance workspace only, the system schemas).
#
# Destroy empties the catalog bucket first, while its bucket policy stands;
# then this stack goes, then the account stack.

module "catalog" {
  source  = "jwallace145/jwall-engineering/aws//modules/databricks-catalog"
  version = "0.18.0"

  providers = {
    databricks = databricks.workspace
  }

  name_root        = var.tags["Organization"]
  project          = var.project
  workspace_id     = var.workspace_id
  admin_group_name = var.admin_group_name
  users_group_name = var.users_group_name

  apply_role_arn       = var.apply_role_arn
  management_role_arns = var.management_role_arns

  tags = var.catalog_tags
}

module "workspace_config" {
  source  = "jwallace145/jwall-engineering/aws//modules/databricks-workspace-config"
  version = "0.18.0"

  providers = {
    databricks = databricks.workspace
  }

  compute_policies = var.compute_policies
  fixed_tags       = var.fixed_tags
  users_group_name = var.users_group_name
  metastore_id     = var.metastore_id

  # System schemas are metastore-wide: only the governance project's workspace
  # enables them.
  enable_system_schemas = var.project == var.governance_project
}
