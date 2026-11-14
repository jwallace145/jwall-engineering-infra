# The sample Databricks project's workspace stack, planned against mock AWS and
# Databricks providers with the account stack's outputs rendered in: the
# rendered inputs (two compute policies of different shapes among them) pass
# the stack's and the modules' validations.
mock_provider "aws" {
  mock_data "aws_caller_identity" {
    defaults = {
      account_id = "650503560686"
      arn        = "arn:aws:iam::650503560686:root"
    }
  }

  mock_data "aws_iam_policy_document" {
    defaults = {
      json = "{}"
    }
  }
}

mock_provider "databricks" {
  alias = "workspace"

  mock_data "databricks_group" {
    defaults = {
      id = "1111"
    }
  }
}

mock_provider "time" {}

# The workspace's current metastore: the one the account stack assigned.
override_data {
  target = module.workspace_config.data.databricks_current_metastore.this
  values = {
    id = "abcdef01-2345-6789-abcd-ef0123456789"
  }
}

override_data {
  target = module.catalog.data.databricks_aws_unity_catalog_policy.uc
  values = {
    json = "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Action\":[\"s3:GetObject\",\"s3:PutObject\",\"s3:DeleteObject\",\"s3:ListBucket\"],\"Resource\":[\"arn:aws:s3:::jwall-engineering-databricks-poc-databricks-catalog/*\",\"arn:aws:s3:::jwall-engineering-databricks-poc-databricks-catalog\"]}]}"
  }
}

override_data {
  target = module.catalog.data.databricks_aws_unity_catalog_assume_role_policy.uc
  values = {
    json = "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Action\":\"sts:AssumeRole\",\"Principal\":{\"AWS\":\"arn:aws:iam::414351767826:role/unity-catalog-prod-UCMasterRole-14S5ZJVKOTYTL\"}}]}"
  }
}

run "plans_the_catalog_and_workspace_config" {
  command = plan

  assert {
    condition     = var.project == var.governance_project
    error_message = "the sample is the governance project, so the plan covers the system schemas"
  }

  assert {
    condition     = toset(keys(var.compute_policies)) == toset(["jobs", "small-interactive"])
    error_message = "the project's two compute policies reach the module"
  }
}

run "plans_a_workspace_that_is_not_the_governance_one" {
  command = plan

  variables {
    governance_project = "another-project"
  }

  assert {
    condition     = var.project != var.governance_project
    error_message = "another project's workspace leaves the system schemas alone"
  }
}
