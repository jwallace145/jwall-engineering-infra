# The Databricks account resources this stack owns, and the CI roles of the
# Databricks workspace projects (config.yaml `databricks`, and
# `projects.<key>.databricks` for each project's guardrails).
#
# Account resources, one of each for the whole account:
#
#   the us-east-1 Unity Catalog metastore   every workspace in the region shares it
#   audit log delivery                      jwall-engineering-databricks-audit-logs, audit/
#
# They wait for config.yaml `databricks.enabled`. While it is false (the
# owner has not created the service principal yet) they are not planned, so
# the foundation plan needs no Databricks credential and the databricks
# provider makes no call. The workflows put the credential in the
# environment only when it is true (.github/actions/databricks-credentials).
#
# The client secret is NOT created here: the owner writes the SSM parameter
# with `jwall-engineering-sites databricks credentials set`, so it is in no
# Terraform state. This stack only builds its ARN for the CI policies.
#
# The registry of Databricks workspace projects (a project file whose only
# component is a databricks-workspace). Each entry gets two CI roles:
#
#   jwall-engineering-<key>-ci-plan   trusted for pull_request on THIS repo     tfstate/read, compute/read, databricks/read
#   jwall-engineering-<key>-ci-apply  trusted for environment:prod on THIS repo tfstate/write, compute/read, compute/manage,
#                                                                               databricks/read, databricks/manage
#
# and no release or task role. Like a bot's, the project's stacks are planned
# and applied from this repository (ci.yml databricks-plan,
# databricks-apply.yml). Both stacks' state (account and workspace) lives
# under project=<key>/ in the shared bucket (state.tf).

locals {
  databricks = local.tool_config.databricks

  databricks_enabled = local.databricks.enabled

  # The one parameter holding the service principal's client secret, and the
  # event-log prefix in the audit bucket (each project appends under
  # <events_prefix>project=<key>/).
  databricks_secret_parameter_arn = "arn:aws:ssm:${local.region}:${local.account_id}:parameter/${trimprefix(local.databricks.secret_parameter, "/")}"
  databricks_events_arn_prefix    = "arn:aws:s3:::${local.databricks.audit_bucket}/${trimsuffix(local.databricks.events_prefix, "/")}/"

  # repository_id: gh api repos/<owner>/<repo> --jq .id
  # One line per entry, as in bots.tf (the workflows read this block).
  databricks_projects = {
    # Empty until the owner creates the repository. Then add databricks-poc
    # with repository jwallace145/databricks-poc and its repository_id.
  }

  databricks_ci = {
    for key, project in local.databricks_projects : key => {
      tfstate = {
        bucket_arn = module.state_bucket.arn
        prefix     = "project=${key}/"
      }
      databricks = {
        secret_parameter_arn = local.databricks_secret_parameter_arn
        events_object_arn    = "${local.databricks_events_arn_prefix}project=${key}/*"
      }
    }
  }
}

module "databricks_metastore" {
  count     = local.databricks_enabled ? 1 : 0
  source    = "jwallace145/jwall-engineering/aws//modules/databricks-metastore"
  version   = "0.18.1"
  providers = { databricks = databricks.account }

  name                             = local.databricks.metastore_name
  admin_user_names                 = local.databricks.metastore_admins
  service_principal_application_id = local.databricks.client_id
}

module "databricks_audit_logs" {
  count     = local.databricks_enabled ? 1 : 0
  source    = "jwallace145/jwall-engineering/aws//modules/databricks-audit-logs"
  version   = "0.18.1"
  providers = { aws = aws, databricks = databricks.account }

  account_id  = local.databricks.account_id
  bucket_name = local.databricks.audit_bucket
}

module "databricks_ci_plan" {
  for_each = local.databricks_projects
  source   = "jwallace145/jwall-engineering/aws//modules/ci-plan-role"
  version  = "0.18.1"

  project           = each.key
  repository        = local.context.repository
  owner_id          = local.github.owner_id
  repository_id     = local.github.repository_id
  oidc_provider_arn = data.aws_iam_openid_connect_provider.github.arn

  policies = ["tfstate/read", "compute/read", "databricks/read"]

  policy_inputs = {
    tfstate    = local.databricks_ci[each.key].tfstate
    databricks = local.databricks_ci[each.key].databricks
  }
}

module "databricks_ci_apply" {
  for_each = local.databricks_projects
  source   = "jwallace145/jwall-engineering/aws//modules/ci-apply-role"
  version  = "0.18.1"

  project           = each.key
  repository        = local.context.repository
  owner_id          = local.github.owner_id
  repository_id     = local.github.repository_id
  oidc_provider_arn = data.aws_iam_openid_connect_provider.github.arn
  environment       = "prod"

  policies = ["tfstate/write", "compute/read", "compute/manage", "databricks/read", "databricks/manage"]

  policy_inputs = {
    tfstate    = local.databricks_ci[each.key].tfstate
    databricks = local.databricks_ci[each.key].databricks
  }
}

# A registered project without its central guardrails would get roles but no
# network range or compute policies; one that is also a site or a bot would
# share (and overwrite) that project's state prefix entry in state.tf.
check "databricks_projects_have_guardrails" {
  assert {
    condition = alltrue([
      for key in keys(local.databricks_projects) :
      try(local.tool_config.projects[key].databricks, null) != null
    ])
    error_message = "Every entry of local.databricks_projects needs a projects.<key>.databricks entry in config.yaml."
  }

  assert {
    condition     = length(setintersection(keys(local.databricks_projects), concat(keys(local.bots), keys(local.projects)))) == 0
    error_message = "A Databricks project must not also be a site or a bot: they would share the project=<key>/ state prefix."
  }
}
