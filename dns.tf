# The jwall.engineering hosted zone. The zone itself is owned here; records
# are owned by the project repos. The only records this stack manages are
# zone-level ones (CAA). The module hard-codes prevent_destroy.

locals {
  zone_name = "jwall.engineering"

  # Certificate authorities allowed to issue for the zone and its subdomains.
  # ACM needs the four Amazon entries. letsencrypt.org stays while
  # survival-guide and guitar are served by GitHub Pages; remove it once both
  # move to CloudFront + ACM.
  caa_issuers = [
    "amazon.com",
    "amazontrust.com",
    "awstrust.com",
    "amazonaws.com",
    "letsencrypt.org",
  ]
}

module "zone" {
  source  = "jwallace145/jwall-engineering/aws//modules/hosted-zone"
  version = "0.5.0"

  name        = local.zone_name
  comment     = "jwall-engineering: zone owned by ${local.context.repository}; records owned by project repos"
  caa_issuers = local.caa_issuers
  name_tag    = "${local.name_prefix}-dns-zone"
}
