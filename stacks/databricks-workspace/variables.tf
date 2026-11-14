# The workspace stack jwall-engineering-sites renders for one Databricks
# project. workspace_url, workspace_id, admin_group_name, users_group_name and
# metastore_id are the account stack's outputs; the rest comes from the
# project's jwall-engineering-sites.yaml and config.yaml. The service
# principal's secret is not a variable: it reaches the provider through the
# environment.

variable "project" {
  description = "Project name in kebab-case, at most 21 characters; the catalog is the project in snake_case."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9]+(-[a-z0-9]+)*$", var.project)) && length(var.project) <= 21
    error_message = "project must be kebab-case and at most 21 characters."
  }
}

variable "environment" {
  description = "Environment of the workspace. Must match `tags.Environment`."
  type        = string

  validation {
    condition     = var.environment == "prod"
    error_message = "environment must be `prod`."
  }
}

variable "repository" {
  description = "GitHub repository of the project, as `owner/repo`. Must match `tags.Repository`."
  type        = string

  validation {
    condition     = can(regex("^[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?/[A-Za-z0-9._-]+$", var.repository))
    error_message = "repository must be an `owner/repo` string."
  }
}

variable "tags" {
  description = "Tags of every AWS resource, through `default_tags`: the same tags as the account stack's."
  type        = map(string)

  validation {
    condition = alltrue([
      for key in ["Organization", "Project", "Environment", "Repository", "ManagedBy", "Owner", "CostCenter", "Workspace"] :
      contains(keys(var.tags), key) && length(var.tags[key]) > 0
    ])
    error_message = "tags must contain non-empty Organization, Project, Environment, Repository, ManagedBy, Owner, CostCenter and Workspace."
  }

  validation {
    condition = (
      lookup(var.tags, "Project", null) == var.project &&
      lookup(var.tags, "Repository", null) == var.repository &&
      lookup(var.tags, "Environment", null) == var.environment &&
      lookup(var.tags, "ManagedBy", null) == "jwall-engineering-infra"
    )
    error_message = "tags.Project, tags.Repository and tags.Environment must equal project, repository and environment, and tags.ManagedBy must be `jwall-engineering-infra`."
  }
}

variable "workspace_url" {
  description = "URL of the workspace (the account stack's workspace_url output): the databricks provider's host."
  type        = string

  validation {
    condition     = can(regex("^https://[a-z0-9.-]+\\.cloud\\.databricks\\.com/?$", var.workspace_url))
    error_message = "workspace_url must be a workspace URL such as https://dbc-01234567-89ab.cloud.databricks.com."
  }
}

variable "workspace_id" {
  description = "Numeric ID of the workspace (the account stack's workspace_id output)."
  type        = number
}

variable "admin_group_name" {
  description = "Account group with ADMIN on the workspace (the account stack's admin_group_name output); it owns the catalog."
  type        = string
}

variable "users_group_name" {
  description = "Account group with USER on the workspace (the account stack's users_group_name output); it uses the catalog and the compute policies."
  type        = string
}

variable "metastore_id" {
  description = "ID of the metastore the workspace is assigned to (the account stack's metastore_id output)."
  type        = string
}

variable "compute_policies" {
  description = "The project's compute policies (projects.<project>.databricks.compute_policies), defined centrally under databricks.compute_policies in config.yaml, by policy name."
  type = map(object({
    description = string
    definition  = string
  }))
}

variable "fixed_tags" {
  description = "Tags pinned on every cluster created through a policy: the account stack's tags."
  type        = map(string)
}

variable "governance_project" {
  description = "The project whose workspace enables the metastore-wide system schemas (databricks.governance_project in config.yaml), or null for none."
  type        = string
  default     = null
}

variable "apply_role_arn" {
  description = "ARN of the project's CI apply role; the catalog bucket lets it list and delete objects so destroy can empty it."
  type        = string
}

variable "management_role_arns" {
  description = "ARNs of the other roles that manage the project (the CI plan role)."
  type        = list(string)
}

variable "catalog_tags" {
  description = "Properties of the catalog: Project and Owner from the project's tags."
  type        = map(string)
}
