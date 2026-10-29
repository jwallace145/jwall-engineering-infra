output "state_bucket_name" {
  description = "Terraform state bucket shared by every jwall-engineering stack."
  value       = module.state_bucket.name
}

output "state_key_convention" {
  description = "State key convention used in the shared bucket."
  value       = "project=<project>/env=<env>/<project>-<env>.tfstate"
}

output "hosted_zone_id" {
  description = "Route 53 hosted zone ID for jwall.engineering."
  value       = module.zone.zone_id
}

output "hosted_zone_name_servers" {
  description = "Name servers delegated for jwall.engineering (for the registrar)."
  value       = module.zone.name_servers
}

output "github_oidc_provider_arn" {
  description = "ARN of the GitHub Actions OIDC identity provider."
  value       = data.aws_iam_openid_connect_provider.github.arn
}

output "infra_ci_plan_role_arn" {
  description = "Role assumed by this repository's pull request plans (AWS_PLAN_ROLE_ARN)."
  value       = module.infra_ci_plan.role_arn
}

output "infra_ci_apply_role_arn" {
  description = "Role assumed by this repository's release applies (AWS_APPLY_ROLE_ARN)."
  value       = module.infra_ci_apply.role_arn
}

output "projects" {
  description = "Per-project bucket name, CI role ARNs and zone ID. Project repos set their AWS_*_ROLE_ARN and bucket variables from these."
  value = {
    for key, project in local.projects : key => {
      repository       = project.repository
      domain           = project.domain
      record_names     = local.project_record_names[key]
      bucket_name      = module.site_bucket[key].name
      plan_role_arn    = module.site_ci_plan[key].role_arn
      preview_role_arn = module.site_ci_preview[key].role_arn
      apply_role_arn   = module.site_ci_apply[key].role_arn
      hosted_zone_id   = module.zone.zone_id
    }
  }
}

output "sites_image_repository_url" {
  description = "ECR repository URL for the jwall-engineering-sites CLI image, for `docker push` and `docker pull`."
  value       = module.sites_image.url
}

output "bots" {
  description = "Per-bot repository, CI role ARNs and state key. The workflows derive these ARNs from config.yaml; this output is for reference."
  value = {
    for key, bot in local.bots : key => {
      repository       = bot.repository
      plan_role_arn    = module.bot_ci_plan[key].role_arn
      apply_role_arn   = module.bot_ci_apply[key].role_arn
      release_role_arn = module.bot_ci_release[key].role_arn
      task_role_arn    = contains(keys(local.bot_task_types), key) ? module.bot_ci_task[key].role_arn : null
      state_key        = "project=${key}/env=prod/${key}-prod.tfstate"
    }
  }
}
