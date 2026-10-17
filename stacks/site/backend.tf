# The CLI writes backend.hcl next to this file at run time, from config.yaml
# (bucket, region) and the site file (the key), and passes it with
# `terraform init -backend-config=backend.hcl`. The block stays empty on purpose.
terraform {
  backend "s3" {}
}
