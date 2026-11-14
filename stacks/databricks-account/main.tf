# The account stack of one Databricks project: a VPC with two private subnets
# and an S3 gateway endpoint, a NAT instance for the subnets' egress, and the
# workspace (root bucket, cross-account role, network, groups, metastore
# assignment). Its outputs are the workspace stack's inputs; the CLI applies
# this stack first.
#
# Replacing the workspace (a new deployment name, network or credentials)
# makes the workspace stack's plan stale; the plan comment says so.

locals {
  name = "${var.tags["Organization"]}-${var.project}"

  # Two /20s (with a /16) in the upper half of the VPC, clear of the public /24s
  # vpc carves from its start; one per availability zone.
  availability_zones   = ["us-east-1a", "us-east-1b"]
  private_subnet_cidrs = [cidrsubnet(var.vpc_cidr, 4, 8), cidrsubnet(var.vpc_cidr, 4, 9)]
}

# The pinned NAT image, resolved from its readable name (databricks.nat_ami in
# config.yaml), so a new Amazon Linux release does not replace the NAT.
# Deprecated images are included so the pin keeps resolving.
data "aws_ami" "nat" {
  owners             = ["amazon"]
  include_deprecated = true
  most_recent        = false

  filter {
    name   = "name"
    values = [var.nat_ami_name]
  }

  filter {
    name   = "architecture"
    values = ["arm64"]
  }
}

# The region's Unity Catalog metastore, owned by the foundation stack.
data "databricks_metastore" "this" {
  provider = databricks.account

  name = var.metastore_name
}

module "network" {
  source  = "jwallace145/jwall-engineering/aws//modules/vpc"
  version = "0.18.1"

  name                 = local.name
  cidr_block           = var.vpc_cidr
  availability_zones   = local.availability_zones
  private_subnet_cidrs = local.private_subnet_cidrs

  # The policy is built from names only, so the endpoint can use the
  # workspace module's output without a cycle.
  s3_gateway_endpoint = {
    enabled = true
    policy  = module.workspace.s3_endpoint_policy
  }
}

module "nat" {
  source  = "jwallace145/jwall-engineering/aws//modules/nat-instance"
  version = "0.18.1"

  name                    = "${local.name}-nat"
  subnet_id               = module.network.public_subnet_ids[0]
  private_route_table_ids = [module.network.private_route_table_id]
  source_cidrs            = local.private_subnet_cidrs
  ami_id                  = data.aws_ami.nat.id
}

module "workspace" {
  source  = "jwallace145/jwall-engineering/aws//modules/databricks-workspace"
  version = "0.18.1"

  providers = {
    databricks = databricks.account
  }

  account_id      = var.account_id
  name_root       = var.tags["Organization"]
  project         = var.project
  environment     = var.environment
  vpc_id          = module.network.vpc_id
  subnet_ids      = module.network.private_subnet_ids
  metastore_id    = data.databricks_metastore.this.metastore_id
  deployment_name = var.deployment_name
  tags            = var.tags

  admin_user_names                 = var.admin_user_names
  user_names                       = var.user_names
  service_principal_application_id = var.service_principal_application_id
}
