# This repository's own CI roles, assumed by GitHub Actions through OIDC.
#
#   jwall-engineering-infra-ci-plan   trusted for pull_request      read-only + state lock
#   jwall-engineering-infra-ci-apply  trusted for environment:prod  manages everything in this stack
#
# The apply role is the highest-privilege identity in the setup. Its IAM
# permissions are restricted to roles and policies named jwall-engineering-*
# and to the GitHub OIDC provider, and the managed policies it may attach are
# allow-listed, so a compromise of the prod environment cannot mint an
# administrator. See README.md, "Why the apply role is scoped the way it is".

locals {
  ci_role_names = {
    plan  = "${local.name_prefix}-ci-plan"
    apply = "${local.name_prefix}-ci-apply"
  }

  github_subject_prefix = "repo:${local.context.repository}"

  # Buckets the apply role may fully manage. The site-bucket module names
  # buckets jwall-engineering-<project>, so one pattern covers every project
  # and adding one to local.projects needs no IAM change.
  managed_bucket_patterns = ["${local.context.organization}-*"]

  managed_bucket_arns = flatten([
    for pattern in local.managed_bucket_patterns : [
      "arn:${local.partition}:s3:::${pattern}",
      "arn:${local.partition}:s3:::${pattern}/*",
    ]
  ])

  managed_role_arn   = "arn:${local.partition}:iam::${local.account_id}:role/${local.context.organization}-*"
  managed_policy_arn = "arn:${local.partition}:iam::${local.account_id}:policy/${local.context.organization}-*"

  # Managed policies the apply role is allowed to attach to the roles it creates.
  attachable_policy_arns = [
    "arn:${local.partition}:iam::aws:policy/ReadOnlyAccess",
    local.managed_policy_arn,
  ]

  state_object_arn = "${aws_s3_bucket.tfstate.arn}/${local.state_key}"
  state_lock_arn   = "${aws_s3_bucket.tfstate.arn}/${local.state_key}.tflock"
}

# ---------------------------------------------------------------------------
# Trust policies
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "ci_plan_assume" {
  statement {
    sid     = "GitHubPullRequests"
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github.arn]
    }

    condition {
      test     = "StringEquals"
      variable = "${local.github_oidc_host}:aud"
      values   = ["sts.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "${local.github_oidc_host}:sub"
      values   = ["${local.github_subject_prefix}:pull_request"]
    }
  }
}

data "aws_iam_policy_document" "ci_apply_assume" {
  statement {
    sid     = "GitHubProdEnvironment"
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github.arn]
    }

    condition {
      test     = "StringEquals"
      variable = "${local.github_oidc_host}:aud"
      values   = ["sts.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "${local.github_oidc_host}:sub"
      values   = ["${local.github_subject_prefix}:environment:prod"]
    }
  }
}

# ---------------------------------------------------------------------------
# Plan role: read everything, read own state, lock own state
# ---------------------------------------------------------------------------

resource "aws_iam_role" "ci_plan" {
  name                 = local.ci_role_names.plan
  description          = "terraform plan for ${local.context.repository} pull requests (read-only)"
  assume_role_policy   = data.aws_iam_policy_document.ci_plan_assume.json
  max_session_duration = 3600

  tags = {
    Name      = local.ci_role_names.plan
    Component = "ci"
  }
}

resource "aws_iam_role_policy_attachment" "ci_plan_readonly" {
  role       = aws_iam_role.ci_plan.name
  policy_arn = "arn:${local.partition}:iam::aws:policy/ReadOnlyAccess"
}

data "aws_iam_policy_document" "ci_plan_state" {
  statement {
    sid       = "ListOwnStatePrefix"
    effect    = "Allow"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.tfstate.arn]

    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["${local.state_key}*"]
    }
  }

  statement {
    sid       = "ReadOwnState"
    effect    = "Allow"
    actions   = ["s3:GetObject", "s3:GetObjectVersion"]
    resources = [local.state_object_arn, local.state_lock_arn]
  }

  statement {
    sid       = "LockOwnState"
    effect    = "Allow"
    actions   = ["s3:PutObject", "s3:DeleteObject"]
    resources = [local.state_lock_arn]
  }
}

resource "aws_iam_role_policy" "ci_plan_state" {
  name   = "${local.ci_role_names.plan}-state"
  role   = aws_iam_role.ci_plan.id
  policy = data.aws_iam_policy_document.ci_plan_state.json
}

# ---------------------------------------------------------------------------
# Apply role: manage everything in this stack
# ---------------------------------------------------------------------------

resource "aws_iam_role" "ci_apply" {
  name                 = local.ci_role_names.apply
  description          = "terraform apply for ${local.context.repository} releases (prod environment)"
  assume_role_policy   = data.aws_iam_policy_document.ci_apply_assume.json
  max_session_duration = 3600

  tags = {
    Name      = local.ci_role_names.apply
    Component = "ci"
  }
}

# Refresh and drift detection read resources Terraform does not otherwise
# touch; ReadOnlyAccess covers that without listing every Get*/List* action.
resource "aws_iam_role_policy_attachment" "ci_apply_readonly" {
  role       = aws_iam_role.ci_apply.name
  policy_arn = "arn:${local.partition}:iam::aws:policy/ReadOnlyAccess"
}

data "aws_iam_policy_document" "ci_apply" {
  # --- Terraform state ------------------------------------------------------
  statement {
    sid       = "ListOwnStatePrefix"
    effect    = "Allow"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.tfstate.arn]

    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["${local.state_key}*"]
    }
  }

  statement {
    sid       = "ReadWriteOwnState"
    effect    = "Allow"
    actions   = ["s3:GetObject", "s3:GetObjectVersion", "s3:PutObject"]
    resources = [local.state_object_arn]
  }

  statement {
    sid       = "LockOwnState"
    effect    = "Allow"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
    resources = [local.state_lock_arn]
  }

  # --- S3: state bucket configuration and project site buckets --------------
  # Bucket-level configuration only on the state bucket (no DeleteBucket, no
  # object access beyond this stack's own state above).
  statement {
    sid    = "ManageStateBucketConfiguration"
    effect = "Allow"
    actions = [
      "s3:GetBucket*",
      "s3:GetEncryptionConfiguration",
      "s3:GetLifecycleConfiguration",
      "s3:PutBucketVersioning",
      "s3:PutEncryptionConfiguration",
      "s3:PutBucketPublicAccessBlock",
      "s3:PutBucketOwnershipControls",
      "s3:PutBucketPolicy",
      "s3:PutBucketTagging",
      "s3:PutLifecycleConfiguration",
    ]
    resources = [aws_s3_bucket.tfstate.arn]
  }

  statement {
    sid       = "ManageSiteBuckets"
    effect    = "Allow"
    actions   = ["s3:*"]
    resources = local.managed_bucket_arns
  }

  statement {
    sid       = "DenyStateBucketDeletion"
    effect    = "Deny"
    actions   = ["s3:DeleteBucket", "s3:DeleteBucketPolicy"]
    resources = [aws_s3_bucket.tfstate.arn]
  }

  # --- IAM: only jwall-engineering-* roles and policies ---------------------
  statement {
    sid    = "ManageOrganisationRoles"
    effect = "Allow"
    actions = [
      "iam:CreateRole",
      "iam:DeleteRole",
      "iam:GetRole",
      "iam:UpdateRole",
      "iam:UpdateRoleDescription",
      "iam:UpdateAssumeRolePolicy",
      "iam:TagRole",
      "iam:UntagRole",
      "iam:ListRoleTags",
      "iam:PutRolePolicy",
      "iam:GetRolePolicy",
      "iam:DeleteRolePolicy",
      "iam:ListRolePolicies",
      "iam:ListAttachedRolePolicies",
      "iam:ListInstanceProfilesForRole",
      "iam:DetachRolePolicy",
    ]
    resources = [local.managed_role_arn]
  }

  # Attaching a managed policy is where privilege escalation would happen, so
  # the attachable policies are allow-listed.
  statement {
    sid       = "AttachAllowListedPolicies"
    effect    = "Allow"
    actions   = ["iam:AttachRolePolicy"]
    resources = [local.managed_role_arn]

    condition {
      test     = "ArnLike"
      variable = "iam:PolicyARN"
      values   = local.attachable_policy_arns
    }
  }

  statement {
    sid    = "ManageOrganisationPolicies"
    effect = "Allow"
    actions = [
      "iam:CreatePolicy",
      "iam:DeletePolicy",
      "iam:GetPolicy",
      "iam:GetPolicyVersion",
      "iam:ListPolicyVersions",
      "iam:CreatePolicyVersion",
      "iam:DeletePolicyVersion",
      "iam:SetDefaultPolicyVersion",
      "iam:TagPolicy",
      "iam:UntagPolicy",
      "iam:ListPolicyTags",
      "iam:ListEntitiesForPolicy",
    ]
    resources = [local.managed_policy_arn]
  }

  statement {
    sid    = "ManageGitHubOidcProvider"
    effect = "Allow"
    actions = [
      "iam:GetOpenIDConnectProvider",
      "iam:TagOpenIDConnectProvider",
      "iam:UntagOpenIDConnectProvider",
      "iam:UpdateOpenIDConnectProviderThumbprint",
      "iam:AddClientIDToOpenIDConnectProvider",
      "iam:RemoveClientIDFromOpenIDConnectProvider",
    ]
    resources = [aws_iam_openid_connect_provider.github.arn]
  }

  # --- Route 53: the jwall.engineering zone only ----------------------------
  statement {
    sid    = "ManageHostedZone"
    effect = "Allow"
    actions = [
      "route53:GetHostedZone",
      "route53:UpdateHostedZoneComment",
      "route53:ListResourceRecordSets",
      "route53:ChangeResourceRecordSets",
      "route53:ChangeTagsForResource",
      "route53:ListTagsForResource",
      "route53:GetDNSSEC",
    ]
    resources = [aws_route53_zone.this.arn]
  }

  statement {
    sid       = "ReadRoute53Changes"
    effect    = "Allow"
    actions   = ["route53:GetChange"]
    resources = ["arn:${local.partition}:route53:::change/*"]
  }
}

resource "aws_iam_role_policy" "ci_apply" {
  name   = "${local.ci_role_names.apply}-manage"
  role   = aws_iam_role.ci_apply.id
  policy = data.aws_iam_policy_document.ci_apply.json
}
