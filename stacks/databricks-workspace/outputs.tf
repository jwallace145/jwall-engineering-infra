output "catalog_name" {
  description = "Name of the project's catalog: the project in snake_case."
  value       = module.catalog.catalog_name
}

output "catalog_bucket_name" {
  description = "Name of the catalog bucket (`<Organization>-<project>-databricks-catalog`)."
  value       = module.catalog.bucket_name
}

output "cluster_policy_ids" {
  description = "IDs of the installed compute policies, by policy name."
  value       = module.workspace_config.cluster_policy_ids
}
