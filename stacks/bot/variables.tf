# The bot stack jwall-engineering-sites renders for one bot project: the
# image repository, a VPC, the records bucket and the bot instance. Every
# value comes from the project's jwall-engineering-sites.yaml (components)
# or from config.yaml; the CLI writes them to terraform.tfvars.json.

variable "project" {
  description = "Project name in kebab-case. Resources are named `<Organization>-<project>[-...]`."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9]+(-[a-z0-9]+)*$", var.project)) && length(var.project) <= 30
    error_message = "project must be kebab-case and at most 30 characters."
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
  description = "Tags applied to every resource through `default_tags`. The six required keys must be present; extra keys pass through."
  type        = map(string)

  validation {
    condition = alltrue([
      for key in ["Organization", "Project", "Environment", "Repository", "ManagedBy", "Owner"] :
      contains(keys(var.tags), key) && length(var.tags[key]) > 0
    ])
    error_message = "tags must contain non-empty Organization, Project, Environment, Repository, ManagedBy and Owner."
  }

  validation {
    condition     = lookup(var.tags, "Project", null) == var.project && lookup(var.tags, "Repository", null) == var.repository
    error_message = "tags.Project must equal project and tags.Repository must equal repository."
  }

  validation {
    condition     = lookup(var.tags, "ManagedBy", null) == "terraform" && lookup(var.tags, "Environment", null) == "prod"
    error_message = "tags.ManagedBy must be `terraform` and tags.Environment must be `prod`."
  }
}

variable "instance_type" {
  description = "EC2 instance type of the bot (lichess-bot.instance_type)."
  type        = string
}

variable "architecture" {
  description = "CPU architecture of the image and the AMI (container-image.architecture)."
  type        = string

  validation {
    condition     = contains(["x86_64", "arm64"], var.architecture)
    error_message = "architecture must be x86_64 or arm64."
  }
}

variable "secret_parameters" {
  description = "Environment variable name to SSM SecureString parameter name (lichess-bot.secrets). The parameters are created out of band by `bot secrets set`."
  type        = map(string)
}

variable "container_environment" {
  description = "Plain environment variables for the bot container (from lichess-bot.engine)."
  type        = map(string)
  default     = {}
}

variable "bot_name" {
  description = "Lichess account the bot plays as (lichess-bot.lichess_user), recorded in every game record's sidecar. Empty when not given."
  type        = string
  default     = ""
}

variable "records_enabled" {
  description = "Whether finished games are shipped to the records bucket (lichess-bot.records.s3)."
  type        = bool
}

variable "records_prefix" {
  description = "Key prefix of the game records in the bucket (bot.records_prefix in config.yaml)."
  type        = string
}

variable "records_container_path" {
  description = "Directory inside the image where lichess-bot writes one PGN per game; fixed by the harness Dockerfile."
  type        = string
}

variable "log_retention_days" {
  description = "Retention of the bot's CloudWatch log group (lichess-bot.logs.retention_days)."
  type        = number
}

variable "health_log_pattern" {
  description = "Line a new container must log to count as healthy (bot.health_log_pattern in config.yaml)."
  type        = string
}

variable "health_seconds" {
  description = "Seconds a new container has to become healthy (bot.health_seconds in config.yaml)."
  type        = number
}

variable "availability_zones" {
  description = "Availability zones of the VPC's public subnets; the bot runs in the first (bot.availability_zones in config.yaml)."
  type        = list(string)
}

variable "keep_release_images" {
  description = "Release images kept in the repository (ecr.keep_release_images in config.yaml)."
  type        = number
}

variable "expire_untagged_after_days" {
  description = "Days after which untagged images expire (ecr.expire_untagged_after_days in config.yaml)."
  type        = number
}
