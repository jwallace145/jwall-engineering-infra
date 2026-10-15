# Adoption of the foundation modules from jwall-engineering/aws 0.4.0. Every
# resource below already exists and stays in place; these blocks only move
# state addresses. Safe to delete once a release has applied them.

moved {
  from = aws_s3_bucket.tfstate
  to   = module.state_bucket.aws_s3_bucket.this
}

moved {
  from = aws_s3_bucket_versioning.tfstate
  to   = module.state_bucket.aws_s3_bucket_versioning.this
}

moved {
  from = aws_s3_bucket_server_side_encryption_configuration.tfstate
  to   = module.state_bucket.aws_s3_bucket_server_side_encryption_configuration.this
}

moved {
  from = aws_s3_bucket_public_access_block.tfstate
  to   = module.state_bucket.aws_s3_bucket_public_access_block.this
}

moved {
  from = aws_s3_bucket_ownership_controls.tfstate
  to   = module.state_bucket.aws_s3_bucket_ownership_controls.this
}

moved {
  from = aws_s3_bucket_policy.tfstate
  to   = module.state_bucket.aws_s3_bucket_policy.this
}

moved {
  from = aws_route53_zone.this
  to   = module.zone.aws_route53_zone.this
}

moved {
  from = aws_route53_record.caa
  to   = module.zone.aws_route53_record.caa[0]
}

moved {
  from = aws_iam_role.ci_plan
  to   = module.infra_ci_roles.module.plan.aws_iam_role.this
}

moved {
  from = aws_iam_role_policy.ci_plan_state
  to   = module.infra_ci_roles.module.plan.aws_iam_role_policy.this["state"]
}

moved {
  from = aws_iam_role_policy_attachment.ci_plan_readonly
  to   = module.infra_ci_roles.module.plan.aws_iam_role_policy_attachment.this["arn:aws:iam::aws:policy/ReadOnlyAccess"]
}

moved {
  from = aws_iam_role.ci_apply
  to   = module.infra_ci_roles.module.apply.aws_iam_role.this
}

moved {
  from = aws_iam_role_policy.ci_apply
  to   = module.infra_ci_roles.module.apply.aws_iam_role_policy.this["manage"]
}

moved {
  from = aws_iam_role_policy_attachment.ci_apply_readonly
  to   = module.infra_ci_roles.module.apply.aws_iam_role_policy_attachment.this["arn:aws:iam::aws:policy/ReadOnlyAccess"]
}
