# The jwall.engineering hosted zone. The zone itself is owned here; records
# are owned by the project repos. The only records this stack manages are
# zone-level ones: CAA and the TXT records that belong to the zone owner
# (domain verification). The module hard-codes prevent_destroy.

locals {
  zone_name = "jwall.engineering"

  # Certificate authorities allowed to issue for the zone and its subdomains.
  # ACM needs these four Amazon entries. Every site is served by CloudFront
  # with an ACM certificate, so no other issuer is allowed.
  caa_issuers = [
    "amazon.com",
    "amazontrust.com",
    "awstrust.com",
    "amazonaws.com",
  ]

  # TXT records that belong to the zone owner, not to a project. The GitHub
  # Pages challenge keeps jwall.engineering verified for the jwallace145
  # account, so no other GitHub user can publish a Pages site on the domain
  # or its subdomains. The token is the one GitHub shows under
  # https://github.com/settings/pages > Verified domains; it stays the same
  # while the domain is registered there. GitHub re-checks the record and
  # unverifies the domain if it is missing for 7 days, so it must stay here.
  txt_records = {
    "_github-pages-challenge-jwallace145" = ["278aa0fd98189307922e6679ba36f1"]
  }
}

module "zone" {
  source  = "jwallace145/jwall-engineering/aws//modules/hosted-zone"
  version = "0.9.1"

  name        = local.zone_name
  comment     = "jwall-engineering: zone owned by ${local.context.repository}; records owned by project repos"
  caa_issuers = local.caa_issuers
  txt_records = local.txt_records
  name_tag    = "${local.name_prefix}-dns-zone"
}
