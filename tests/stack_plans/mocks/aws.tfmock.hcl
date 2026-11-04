# What the bot stack and its modules read from AWS at plan. Nothing here calls
# AWS: scripts/test-stack-plans.sh runs these tests against a mock provider.
mock_data "aws_ami" {
  defaults = {
    id           = "ami-0123456789abcdef0"
    architecture = "x86_64"
  }
}

mock_data "aws_caller_identity" {
  defaults = {
    account_id = "650503560686"
    arn        = "arn:aws:iam::650503560686:root"
  }
}

mock_data "aws_region" {
  defaults = {
    region = "us-east-1"
    name   = "us-east-1"
  }
}

mock_data "aws_iam_policy_document" {
  defaults = {
    json = "{}"
  }
}
