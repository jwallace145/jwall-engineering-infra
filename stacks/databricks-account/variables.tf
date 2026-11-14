# The account stack jwall-engineering-sites renders for one Databricks project.
# Every value comes from the project's jwall-engineering-sites.yaml or from
# config.yaml (projects.<project>.databricks and the databricks section); the
# CLI writes them to terraform.tfvars.json. The service principal's secret is
# not a variable: it reaches the provider through the environment.

variable "project" {
  description = "Project name in kebab-case, at most 21 characters (`<Organization>-<project>-databricks-cross-account` must fit IAM's 64)."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9]+(-[a-z0-9]+)*$", var.project)) && length(var.project) <= 21
    error_message = "project must be kebab-case and at most 21 characters."
  }
}

variable "environment" {
  description = "Environment of the workspace; the workspace is named `<project>-<environment>`."
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
  description = "Tags of every AWS resource (through `default_tags`) and of the workspace's clusters: the project file's tags plus CostCenter (config.yaml) and Workspace."
  type        = map(string)

  validation {
    condition = alltrue([
      for key in ["Organization", "Project", "Environment", "Repository", "ManagedBy", "Owner", "CostCenter", "Workspace"] :
      contains(keys(var.tags), key) && length(var.tags[key]) > 0
    ])
    error_message = "tags must contain non-empty Organization, Project, Environment, Repository, ManagedBy, Owner, CostCenter and Workspace."
  }

  validation {
    condition     = lookup(var.tags, "Project", null) == var.project && lookup(var.tags, "Repository", null) == var.repository
    error_message = "tags.Project must equal project and tags.Repository must equal repository."
  }

  validation {
    condition = (
      lookup(var.tags, "ManagedBy", null) == "jwall-engineering-infra" &&
      lookup(var.tags, "Environment", null) == var.environment &&
      lookup(var.tags, "Workspace", null) == "${var.project}-${var.environment}"
    )
    error_message = "tags.ManagedBy must be `jwall-engineering-infra`, tags.Environment must equal environment and tags.Workspace must be `<project>-<environment>`."
  }
}

variable "vpc_cidr" {
  description = "IPv4 CIDR of the project's VPC (projects.<project>.databricks.vpc_cidr in config.yaml). The private subnets are its 9th and 10th sixteenths, so its prefix is /16 to /22 (Databricks needs /17 to /26 subnets)."
  type        = string

  validation {
    condition     = can(cidrhost(var.vpc_cidr, 0)) && can(regex("^[0-9.]+/(1[6-9]|2[0-2])$", var.vpc_cidr))
    error_message = "vpc_cidr must be an IPv4 CIDR with a prefix length between 16 and 22, for example `10.40.0.0/16`."
  }
}

variable "nat_ami_name" {
  description = "Name of the Amazon-owned arm64 image the NAT instance is pinned to (databricks.nat_ami in config.yaml). Changing it replaces the NAT."
  type        = string

  validation {
    condition     = length(var.nat_ami_name) > 0
    error_message = "nat_ami_name must not be empty."
  }
}

variable "metastore_name" {
  description = "Name of the region's Unity Catalog metastore the workspace is assigned to (databricks.metastore_name in config.yaml)."
  type        = string

  validation {
    condition     = length(var.metastore_name) > 0
    error_message = "metastore_name must not be empty."
  }
}

variable "deployment_name" {
  description = "First label of the workspace URL, or null to let Databricks pick it (databricks.deployment_name_prefix in config.yaml is null). Changing it replaces the workspace."
  type        = string
  default     = null
}

variable "admin_user_names" {
  description = "Email addresses of the workspace admins (projects.<project>.databricks.admins). Each must already be a user of the account."
  type        = list(string)
}

variable "user_names" {
  description = "Email addresses of the workspace's other users (projects.<project>.databricks.users). Each must already be a user of the account."
  type        = list(string)
  default     = []
}

variable "service_principal_application_id" {
  description = "Application (client) ID of the automation service principal (databricks.client_id in config.yaml); it joins the admin group so the workspace stack can manage the workspace."
  type        = string
}

variable "account_id" {
  description = "Databricks account ID (databricks.account_id in config.yaml), also the cross-account role's external ID. The provider reads the same ID from DATABRICKS_ACCOUNT_ID."
  type        = string
}
