# One bot project: the image repository its releases are pushed to, a VPC
# with public subnets (egress only, no NAT), the records bucket the games are
# shipped to, and the instance that runs the bot. With a task-runner
# component, also the task launch template and the task results bucket
# (ephemeral CI task instances are launched by `task run`, never by Terraform).
# Each further task profile (task-runner.profiles, e.g. gpu) adds its own
# launch template and instance role. With dataset components, one
# kept-forever, append-only bucket each.
#
# Changing the instance's inputs replaces the instance and ends the game in
# progress: its image (pinned in config.yaml, bot.amis), its type, or its user
# data (a library bump can change that). Read the plan comment and apply bot
# stacks by hand while the bot is idle.
#
# The CI roles live in the foundation stack (foundation/bots.tf).

locals {
  name = "${var.tags["Organization"]}-${var.project}"
}

# The pinned image, resolved from its readable name. Names are unique among
# Amazon-owned images; deprecated images are included so a pin keeps resolving
# after Amazon deprecates it.
data "aws_ami" "pinned" {
  count = var.ami_name == null ? 0 : 1

  owners             = ["amazon"]
  include_deprecated = true
  most_recent        = false

  filter {
    name   = "name"
    values = [var.ami_name]
  }

  filter {
    name   = "architecture"
    values = [var.architecture]
  }
}

# The pinned image of each task profile that names one (tasks.images in
# config.yaml), resolved like the bot's.
data "aws_ami" "task_profile" {
  for_each = { for p in var.task_profiles : p.name => p if p.ami_name != null }

  owners             = ["amazon"]
  include_deprecated = true
  most_recent        = false

  filter {
    name   = "name"
    values = [each.value.ami_name]
  }

  filter {
    name   = "architecture"
    values = [var.architecture]
  }
}

module "image" {
  source  = "jwallace145/jwall-engineering/aws//modules/ecr-repository"
  version = "0.16.0"

  name                       = local.name
  keep_release_images        = var.keep_release_images
  expire_untagged_after_days = var.expire_untagged_after_days
}

module "network" {
  source  = "jwallace145/jwall-engineering/aws//modules/vpc"
  version = "0.16.0"

  name               = local.name
  availability_zones = var.availability_zones
}

module "records" {
  source  = "jwallace145/jwall-engineering/aws//modules/private-bucket"
  version = "0.16.0"
  count   = var.records_enabled ? 1 : 0

  name = "${local.name}-records"
}

module "bot" {
  source  = "jwallace145/jwall-engineering/aws//modules/container-instance"
  version = "0.16.0"

  name                 = "${local.name}-bot"
  project              = var.project
  name_root            = var.tags["Organization"]
  image_repository_url = module.image.url
  image_repository_arn = module.image.arn
  instance_type        = var.instance_type
  architecture         = var.architecture
  ami_id               = var.ami_name == null ? null : data.aws_ami.pinned[0].id
  vpc_id               = module.network.vpc_id
  subnet_id            = module.network.public_subnet_ids[0]

  secret_parameters     = var.secret_parameters
  container_environment = var.container_environment
  bot_name              = var.bot_name

  records_bucket_name    = var.records_enabled ? module.records[0].name : null
  records_prefix         = var.records_prefix
  records_container_path = var.records_enabled ? var.records_container_path : null

  log_retention_days = var.log_retention_days
  health_log_pattern = var.health_log_pattern
  health_seconds     = var.health_seconds
}

# Task outputs, written by task instances under tasks/<run>/<task>/ and copied
# back by `task run`. Expired after task_results_days.
module "task_results" {
  source  = "jwallace145/jwall-engineering/aws//modules/private-bucket"
  version = "0.16.0"
  count   = var.task_runner_enabled ? 1 : 0

  name                           = "${local.name}-tasks"
  current_object_expiration_days = var.task_results_days
}

module "tasks" {
  source  = "jwallace145/jwall-engineering/aws//modules/task-runner"
  version = "0.16.0"
  count   = var.task_runner_enabled ? 1 : 0

  name_root             = var.tags["Organization"]
  project               = var.project
  vpc_id                = module.network.vpc_id
  architecture          = var.architecture
  ami_id                = var.ami_name == null ? null : data.aws_ami.pinned[0].id
  default_instance_type = var.task_instance_type
  max_lifetime_hours    = var.task_max_lifetime_hours
  log_retention_days    = var.task_log_retention_days
  rust_toolchain        = var.task_rust_toolchain
}

# The other task profiles: their own template, image, disk and instance role;
# the cpu profile's log group, security group and results bucket.
module "task_profiles" {
  source   = "jwallace145/jwall-engineering/aws//modules/task-runner"
  version  = "0.16.0"
  for_each = { for p in var.task_profiles : p.name => p }

  name_root             = var.tags["Organization"]
  project               = var.project
  profile               = each.key
  shared                = module.tasks[0].shared
  vpc_id                = module.network.vpc_id
  architecture          = var.architecture
  ami_id                = each.value.ami_name == null ? (var.ami_name == null ? null : data.aws_ami.pinned[0].id) : data.aws_ami.task_profile[each.key].id
  default_instance_type = each.value.default_instance_type
  root_volume_gb        = each.value.root_volume_gb
  gpu                   = each.value.gpu
  max_lifetime_hours    = each.value.max_hours
  log_retention_days    = var.task_log_retention_days
  rust_toolchain        = var.task_rust_toolchain
}

# Kept-forever, append-only datasets (for example NNUE training data). Task
# instances may add objects under the prefix when the dataset lists
# task-runner as a writer; nothing automated may delete them. A task profile
# granted a dataset reads and adds only under its own sub-prefixes.
module "datasets" {
  source   = "jwallace145/jwall-engineering/aws//modules/dataset-bucket"
  version  = "0.16.0"
  for_each = { for d in var.datasets : d.name => d }

  name             = each.value.bucket
  prefix           = each.value.prefix
  transition_days  = each.value.transition_days
  reader_role_arns = each.value.readers
  writer_role_names = [
    for w in each.value.writers : module.tasks[0].instance_role_name if w == "task-runner"
  ]
  role_grants = {
    for p in var.task_profiles : module.task_profiles[p.name].instance_role_name => {
      read  = one([for d in p.datasets : d.read if d.name == each.key])
      write = one([for d in p.datasets : d.write if d.name == each.key])
    } if contains([for d in p.datasets : d.name], each.key)
  }
}
