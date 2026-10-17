provider "aws" {
  region = "us-east-1"

  # Every resource carries the site file's tags. The Project tag is what the
  # apply role's IAM policies key on, so this block is load-bearing.
  default_tags {
    tags = var.tags
  }
}
