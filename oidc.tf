# GitHub Actions OIDC identity provider.
#
# An account can hold only one IAM OIDC provider per URL, so every stack in
# this account shares this one. It is currently owned by the prog-strength
# infra stack (tagged ManagedBy=prog-strength-infra), so this stack only
# references it. Sharing is safe: the provider grants nothing by itself, and
# each role's trust policy restricts which repository/environment may assume
# it.
#
# TODO(owner): move ownership of the provider into this stack. Steps:
#   1. In prog-strength-infra, `terraform state rm` the provider and replace
#      it with a data source like the one below.
#   2. Here, replace the data source with
#        import { to = aws_iam_openid_connect_provider.github, id = <arn> }
#        resource "aws_iam_openid_connect_provider" "github" { url = ..., client_id_list = ["sts.amazonaws.com"] }
#      and give the apply role (infra-ci-roles module) permission to manage it.
#   3. Update README.md and docs/migration.md.

data "aws_iam_openid_connect_provider" "github" {
  url = "https://token.actions.githubusercontent.com"
}
