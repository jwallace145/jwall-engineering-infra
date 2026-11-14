#!/usr/bin/env bash
# Prints the Databricks project registry, local.databricks_projects in
# foundation/databricks.tf, as a JSON array of {"project", "repository"}: the
# matrix of ci.yml's databricks-plan job and the lookup of databricks-apply.yml.
# Entries are one line each, as in bots.tf. Fails when the block is missing.
#
# Usage: scripts/databricks-projects.sh [path/to/databricks.tf]
set -euo pipefail

file=${1:-foundation/databricks.tf}
grep -q '^  databricks_projects = {$' "$file" || {
  echo "no multi-line databricks_projects = { ... } local in $file" >&2
  exit 1
}
awk '/^  databricks_projects = \{$/ { inside = 1; next } inside && /^  \}/ { exit } inside' "$file" |
  sed -n 's/^ *\([a-z0-9-][a-z0-9-]*\) *= *{ *repository *= *"\([^"]*\)".*/\1 \2/p' |
  jq -Rnc '[inputs | split(" ") | {project: .[0], repository: .[1]}]'
