# The registry of bot projects (jwall-engineering-sites.yaml with
# `components`: a container image and a Lichess bot). Adding a bot means
# adding a line here; each entry gets three CI roles, and a fourth with a
# task runner (local.bot_task_types):
#
#   jwall-engineering-<key>-ci-plan     trusted for pull_request on THIS repo     tfstate/read, container/read, task/read
#                                                                                 (+ dataset/read with datasets)
#   jwall-engineering-<key>-ci-apply    trusted for environment:prod on THIS repo tfstate/write, container/read, container/manage, task/read, task/manage
#                                                                                 (+ dataset/read, dataset/manage with datasets)
#   jwall-engineering-<key>-ci-release  trusted for the bot repo's v* tags only   container/release
#   jwall-engineering-<key>-ci-task     trusted for the bot repo's pull_request   task/launch, for every task profile
#                                       and main only (never forks)
#
# The bot's infrastructure is planned and applied from this repository (the
# consumer keeps no Terraform and no infra workflow): a bot stack apply can
# replace the instance, so it is a deliberate act here, not a consumer push.
# The consumer's own release workflow only ships images and rolls them out,
# with the release role, which can push to the bot's image repository, write
# /jwall-engineering/<key>/bot/* parameters and run the reconciler, and
# nothing else (it cannot read the Lichess token).
#
# State lives in the shared bucket under project=<key>/ (state.tf).

locals {
  # repository_id: gh api repos/<owner>/<repo> --jq .id
  bots = {
    rusty-chess = { repository = "jwallace145/rusty-chess-v2", repository_id = "1435162673" }
  }

  # Bots with a task-runner component, and the instance types their task
  # launch role may start: the project file's task-runner.instance_types,
  # then each profile's.
  bot_task_types = {
    rusty-chess = ["c8a.xlarge", "c8a.8xlarge", "c8a.16xlarge", "g6.xlarge", "g6.2xlarge", "g6e.xlarge"]
  }

  # Their task profiles: cpu (the task-runner's own fields), then the
  # project file's task-runner.profiles. The launch role may pass each
  # profile's instance role, <org>-<key>-task[-<profile>].
  bot_task_profiles = {
    rusty-chess = ["cpu", "gpu"]
  }

  # Bots with dataset components, and their buckets: the project file's
  # dataset buckets, <Organization>-<project>-<name>. Their plan and apply
  # roles may read and configure these buckets, never their objects.
  bot_datasets = {
    rusty-chess = ["jwall-engineering-rusty-chess-nnue"]
  }

  bot_tfstate = {
    for key, bot in local.bots : key => {
      bucket_arn = module.state_bucket.arn
      prefix     = "project=${key}/"
    }
  }
}

module "bot_ci_plan" {
  for_each = local.bots
  source   = "jwallace145/jwall-engineering/aws//modules/ci-plan-role"
  version  = "0.16.1"

  project           = each.key
  repository        = local.context.repository
  owner_id          = local.github.owner_id
  repository_id     = local.github.repository_id
  oidc_provider_arn = data.aws_iam_openid_connect_provider.github.arn

  policies = concat(
    ["tfstate/read", "container/read", "task/read"],
    contains(keys(local.bot_datasets), each.key) ? ["dataset/read"] : [],
  )

  policy_inputs = {
    tfstate = local.bot_tfstate[each.key]
    dataset = contains(keys(local.bot_datasets), each.key) ? { bucket_names = local.bot_datasets[each.key] } : null
  }
}

module "bot_ci_apply" {
  for_each = local.bots
  source   = "jwallace145/jwall-engineering/aws//modules/ci-apply-role"
  version  = "0.16.1"

  project           = each.key
  repository        = local.context.repository
  owner_id          = local.github.owner_id
  repository_id     = local.github.repository_id
  oidc_provider_arn = data.aws_iam_openid_connect_provider.github.arn
  environment       = "prod"

  policies = concat(
    ["tfstate/write", "container/read", "container/manage", "task/read", "task/manage"],
    contains(keys(local.bot_datasets), each.key) ? ["dataset/read", "dataset/manage"] : [],
  )

  policy_inputs = {
    tfstate = local.bot_tfstate[each.key]
    dataset = contains(keys(local.bot_datasets), each.key) ? { bucket_names = local.bot_datasets[each.key] } : null
  }
}

module "bot_ci_release" {
  for_each = local.bots
  source   = "jwallace145/jwall-engineering/aws//modules/ci-release-role"
  version  = "0.16.1"

  project           = each.key
  repository        = each.value.repository
  owner_id          = local.github_owner_id
  repository_id     = each.value.repository_id
  oidc_provider_arn = data.aws_iam_openid_connect_provider.github.arn

  policies = ["container/release"]
}

# Launches, runs and terminates the bot's ephemeral CI task instances from
# the consumer's own workflows (`task run`). Trusted for the consumer's
# pull_request and main events only: GitHub issues no OIDC token to fork
# pull requests, and the remote-task workflow refuses every other event.
module "bot_ci_task" {
  for_each = local.bot_task_types
  source   = "jwallace145/jwall-engineering/aws//modules/ci-task-role"
  version  = "0.16.1"

  project           = each.key
  repository        = local.bots[each.key].repository
  owner_id          = local.github_owner_id
  repository_id     = local.bots[each.key].repository_id
  oidc_provider_arn = data.aws_iam_openid_connect_provider.github.arn

  policies = ["task/launch"]

  policy_inputs = {
    task = { instance_types = each.value, profiles = local.bot_task_profiles[each.key] }
  }
}
