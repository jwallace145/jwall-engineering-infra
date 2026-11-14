# The workspace stack's inputs: the CLI reads these with `terraform output -json`
# and renders them into the workspace stack's tfvars under the same names.

output "workspace_id" {
  description = "Numeric ID of the workspace."
  value       = module.workspace.workspace_id
}

output "workspace_url" {
  description = "URL of the workspace: the host of the workspace stack's databricks provider."
  value       = module.workspace.workspace_url
}

output "admin_group_name" {
  description = "Account group with ADMIN on the workspace (`<project>-workspace-admins`)."
  value       = module.workspace.admin_group_name
}

output "users_group_name" {
  description = "Account group with USER on the workspace (`<project>-workspace-users`)."
  value       = module.workspace.users_group_name
}

output "root_bucket_name" {
  description = "Name of the workspace root bucket (`<Organization>-<project>-databricks-root`)."
  value       = module.workspace.root_bucket_name
}

output "metastore_id" {
  description = "ID of the Unity Catalog metastore the workspace is assigned to."
  value       = data.databricks_metastore.this.metastore_id
}
