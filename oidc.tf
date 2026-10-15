# GitHub Actions OIDC identity provider. The account already has one (the
# main site's deploy roles trust it), so it is adopted rather than created.
# Terraform ignores the import block once the resource is in state.

locals {
  github_oidc_host = "token.actions.githubusercontent.com"
  github_oidc_url  = "https://${local.github_oidc_host}"
}

import {
  to = aws_iam_openid_connect_provider.github
  id = "arn:${local.partition}:iam::${local.account_id}:oidc-provider/${local.github_oidc_host}"
}

resource "aws_iam_openid_connect_provider" "github" {
  url            = local.github_oidc_url
  client_id_list = ["sts.amazonaws.com"]

  # AWS validates GitHub's tokens against its own trust store and ignores the
  # thumbprint list; leaving it unset keeps the imported value as-is.

  tags = {
    Name      = "${local.name_prefix}-github-oidc"
    Component = "github-oidc"
  }
}
