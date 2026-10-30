output "image_repository_url" {
  description = "ECR repository the release images are pushed to."
  value       = module.image.url
}

output "instance_id" {
  description = "The bot's EC2 instance, for Session Manager."
  value       = module.bot.instance_id
}

output "public_ip" {
  description = "The bot's public IPv4 address (egress only)."
  value       = module.bot.public_ip
}

output "log_group_name" {
  description = "CloudWatch log group of the bot container."
  value       = module.bot.log_group_name
}

output "parameter_names" {
  description = "SSM parameters shared by the release tooling and the instance: desired_version, running_version, status, last_sync."
  value       = module.bot.parameter_names
}

output "records_uri" {
  description = "Where finished games are shipped, or null without records."
  value       = module.bot.records_uri
}

output "vpc_id" {
  description = "The project's VPC."
  value       = module.network.vpc_id
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
