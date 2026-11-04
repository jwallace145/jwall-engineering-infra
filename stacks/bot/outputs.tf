output "image_repository_url" {
  description = "ECR repository the release images are pushed to, or null without a bot."
  value       = try(module.image[0].url, null)
}

output "instance_id" {
  description = "The bot's EC2 instance, for Session Manager, or null without a bot."
  value       = try(module.bot[0].instance_id, null)
}

output "public_ip" {
  description = "The bot's public IPv4 address (egress only), or null without a bot."
  value       = try(module.bot[0].public_ip, null)
}

output "log_group_name" {
  description = "CloudWatch log group of the bot container, or null without a bot."
  value       = try(module.bot[0].log_group_name, null)
}

output "parameter_names" {
  description = "SSM parameters shared by the release tooling and the instance: desired_version, running_version, status, last_sync. Null without a bot."
  value       = try(module.bot[0].parameter_names, null)
}

output "records_uri" {
  description = "Where finished games are shipped, or null without a bot or records."
  value       = try(module.bot[0].records_uri, null)
}

output "vpc_id" {
  description = "The project's VPC, or null without a bot or a task runner."
  value       = try(module.network[0].vpc_id, null)
}

output "task_launch_template" {
  description = "Name of the task launch template, or null without a task runner."
  value       = var.task_runner_enabled ? module.tasks[0].launch_template_name : null
}

output "task_log_group" {
  description = "Log group task output streams to, or null without a task runner."
  value       = var.task_runner_enabled ? module.tasks[0].log_group_name : null
}

output "task_results_uri" {
  description = "Where task outputs land, as <uri><run>/<task>/, or null without a task runner."
  value       = var.task_runner_enabled ? module.tasks[0].results_uri : null
}

output "dataset_uris" {
  description = "Where each dataset's data lives, by name: s3://<bucket>/<prefix>."
  value       = { for name, d in module.datasets : name => d.uri }
}

output "task_profile_launch_templates" {
  description = "Launch template of each task profile beyond cpu, by profile name."
  value       = { for name, p in module.task_profiles : name => p.launch_template_name }
}
