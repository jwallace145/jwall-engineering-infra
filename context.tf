# Identity of this stack: who owns it, which repo manages it, and the tag set
# that default_tags stamps on everything. Derived from the library's context
# module so every jwall-engineering stack tags resources the same way.

locals {
  region = "us-east-1"

  context = {
    organization = "jwall-engineering"
    project      = "jwall-engineering-infra" # Project tag value
    environment  = "prod"
    repository   = "jwallace145/jwall-engineering-infra"
    owner        = "jwallace145"
  }

  # Numeric GitHub IDs for the immutable OIDC subject form
  # (repo:<owner>@<owner_id>/<name>@<repo_id>:...), which GitHub issues when
  # "use immutable subject" is on for the repository (the default for new
  # repos). Look up with: gh api repos/<owner>/<repo> --jq '{id, owner_id: .owner.id}'
  github = {
    owner_id      = 35950238
    repository_id = 1420037633
  }

  # Resource name prefix for this stack's own CI roles and Name tags. The
  # role names are fixed by convention (jwall-engineering-infra-ci-*), so this
  # is intentionally not module.context.name_prefix, which adds "-prod".
  name_prefix = local.context.project
}

module "context" {
  source  = "jwallace145/jwall-engineering/aws//modules/context"
  version = "0.7.0"

  project      = local.context.project
  environment  = local.context.environment
  repository   = local.context.repository
  owner        = local.context.owner
  organization = local.context.organization
}
