output "state_bucket_name" {
  description = "Terraform state bucket shared by every jwall-engineering stack."
  value       = aws_s3_bucket.tfstate.bucket
}

output "state_key_convention" {
  description = "State key convention used in the shared bucket."
  value       = "project=<project>/env=<env>/<project>-<env>.tfstate"
}

output "hosted_zone_id" {
  description = "Route 53 hosted zone ID for jwall.engineering."
  value       = aws_route53_zone.this.zone_id
}

output "hosted_zone_name_servers" {
  description = "Name servers delegated for jwall.engineering (for the registrar)."
  value       = aws_route53_zone.this.name_servers
}

output "github_oidc_provider_arn" {
  description = "ARN of the GitHub Actions OIDC identity provider."
  value       = data.aws_iam_openid_connect_provider.github.arn
}

output "infra_ci_plan_role_arn" {
  description = "Role assumed by this repository's pull request plans (AWS_PLAN_ROLE_ARN)."
  value       = aws_iam_role.ci_plan.arn
}

output "infra_ci_apply_role_arn" {
  description = "Role assumed by this repository's release applies (AWS_APPLY_ROLE_ARN)."
  value       = aws_iam_role.ci_apply.arn
}

output "projects" {
  description = "Per-project bucket name, CI role ARNs and zone ID. Project repos set their AWS_*_ROLE_ARN and bucket variables from these."
  value = {
    for key, project in local.projects : key => {
      repository       = project.repository
      domain           = project.domain
      record_names     = local.project_record_names[key]
      bucket_name      = module.site_bucket[key].name
      plan_role_arn    = module.site_ci_roles[key].plan_role_arn
      preview_role_arn = module.site_ci_roles[key].preview_role_arn
      apply_role_arn   = module.site_ci_roles[key].apply_role_arn
      hosted_zone_id   = aws_route53_zone.this.zone_id
    }
  }
}
