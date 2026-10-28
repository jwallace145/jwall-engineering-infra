# One bot project: the image repository its releases are pushed to, a VPC
# with public subnets (egress only, no NAT), the records bucket the games are
# shipped to, and the instance that runs the bot.
#
# Changing the instance's inputs (or a library bump that changes its user
# data or AMI) replaces the instance and ends the game in progress. Read the
# plan comment and apply bot stacks by hand while the bot is idle.
#
# The CI roles live in the foundation stack (foundation/bots.tf).

locals {
  name = "${var.tags["Organization"]}-${var.project}"
}

module "image" {
  source  = "jwallace145/jwall-engineering/aws//modules/ecr-repository"
  version = "0.10.0"

  name                       = local.name
  keep_release_images        = var.keep_release_images
  expire_untagged_after_days = var.expire_untagged_after_days
}

module "network" {
  source  = "jwallace145/jwall-engineering/aws//modules/vpc"
  version = "0.10.0"

  name               = local.name
  availability_zones = var.availability_zones
}

module "records" {
  source  = "jwallace145/jwall-engineering/aws//modules/private-bucket"
  version = "0.10.0"
  count   = var.records_enabled ? 1 : 0

  name = "${local.name}-records"
}

module "bot" {
  source  = "jwallace145/jwall-engineering/aws//modules/container-instance"
  version = "0.10.0"

  name                 = "${local.name}-bot"
  project              = var.project
  name_root            = var.tags["Organization"]
  image_repository_url = module.image.url
  image_repository_arn = module.image.arn
  instance_type        = var.instance_type
  architecture         = var.architecture
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
