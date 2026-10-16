output "bucket_name" {
  description = "Site bucket the publish and preview commands sync into."
  value       = local.bucket_name
}

output "prod_url" {
  description = "Production URL."
  value       = module.site.prod_url
}

output "prod_distribution_id" {
  description = "CloudFront distribution ID for production, invalidated after each publish."
  value       = module.site.prod_distribution_id
}

output "dev_url" {
  description = "Dev URL, or null without a dev environment."
  value       = module.site.dev_url
}

output "dev_distribution_id" {
  description = "CloudFront distribution ID for dev, or null without a dev environment."
  value       = module.site.dev_distribution_id
}

output "preview_url_pattern" {
  description = "Preview URL pattern (`https://pr-<n>.preview.<domain_name>`), or null when previews are disabled."
  value       = module.site.preview_url_pattern
}

output "preview_distribution_id" {
  description = "CloudFront distribution ID serving `*.preview.<domain_name>`, or null when previews are disabled."
  value       = module.site.preview_distribution_id
}
