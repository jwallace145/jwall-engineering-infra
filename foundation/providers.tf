provider "aws" {
  region = local.region

  # Every resource this stack creates carries the organisation-wide tag set.
  # Per-resource `tags` add `Component` and `Name` on top.
  default_tags {
    tags = module.context.tags
  }
}
