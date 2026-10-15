# The jwall.engineering hosted zone. The zone itself is owned here; records
# are owned by the project repos. The only records this stack manages are
# zone-level ones (CAA).
#
# The zone already exists (it was created by the main site's Terraform), so it
# is adopted with an import block. Its ID is looked up by name.

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

data "aws_route53_zone" "existing" {
  name         = local.zone_name
  private_zone = false
}

import {
  to = aws_route53_zone.this
  id = data.aws_route53_zone.existing.zone_id
}

resource "aws_route53_zone" "this" {
  name    = local.zone_name
  comment = "jwall-engineering: zone owned by ${local.context.repository}; records owned by project repos"

  tags = {
    Name      = "${local.name_prefix}-dns-zone"
    Component = "dns"
  }

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_route53_record" "caa" {
  zone_id = aws_route53_zone.this.zone_id
  name    = local.zone_name
  type    = "CAA"
  ttl     = 300

  records = [for issuer in local.caa_issuers : "0 issue \"${issuer}\""]
}
