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

variable "ami_name" {
  description = "Name of the Amazon-owned image the bot instance is pinned to (bot.amis.<architecture> in config.yaml), e.g. al2023-ami-2023.9.20251027.0-kernel-6.1-x86_64. Changing it replaces the instance; null follows the latest Amazon Linux 2023 image."
  type        = string
  default     = null
}

variable "task_runner_enabled" {
  description = "Whether the project has a task-runner component: the task launch template, its instance role and the task results bucket."
  type        = bool
}

variable "task_instance_type" {
  description = "Default instance type of task instances (task-runner.default_instance_type); `task run` passes the type per launch."
  type        = string
}

variable "task_max_lifetime_hours" {
  description = "Hours after boot at which a task instance shuts itself down and terminates (task-runner.max_hours)."
  type        = number
}

variable "task_results_days" {
  description = "Days task outputs stay in the task results bucket (task-runner.results_days)."
  type        = number
}

variable "task_log_retention_days" {
  description = "Retention of the task log group (task-runner.log_retention_days)."
  type        = number
}

variable "task_rust_toolchain" {
  description = "Rust toolchain task instances install (tasks.rust_toolchain in config.yaml)."
  type        = string
}

variable "datasets" {
  description = "The project's dataset components: kept-forever, append-only buckets named `<Organization>-<project>-<name>`. `writers` lists the components whose roles may add objects under `prefix` (`task-runner`: the task instances); `readers` lists role ARNs that may read under it."
  type = list(object({
    name            = string
    bucket          = string
    prefix          = string
    writers         = list(string)
    readers         = list(string)
    transition_days = number
  }))
  default = []

  validation {
    condition     = alltrue([for d in var.datasets : d.bucket == "${var.tags["Organization"]}-${var.project}-${d.name}"])
    error_message = "each dataset's bucket must be <Organization>-<project>-<name>."
  }

  validation {
    condition     = alltrue([for d in var.datasets : alltrue([for w in d.writers : w == "task-runner"])])
    error_message = "dataset writers may only be task-runner."
  }

  validation {
    condition     = var.task_runner_enabled || !anytrue([for d in var.datasets : contains(d.writers, "task-runner")])
    error_message = "a dataset written by task-runner needs the task-runner component."
  }
}
