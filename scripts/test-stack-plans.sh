#!/usr/bin/env bash
# Plans each stack that has tests under tests/stack_plans/<stack>/ (bot,
# databricks-account, databricks-workspace) against mock providers, with tfvars
# rendered by the CLI's own code for each case. This proves the rendered inputs
# pass the stacks' and the modules' validations; it touches no AWS or Databricks.
#
# It works on a temporary copy of each stack, as the CLI does, so the test files
# never sit in stacks/: a real plan and the image's stacks do not see them.
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

(cd "$root" && uv run python -m tests.stack_plans.render "$work/rendered")

status=0
for tests in "$root"/tests/stack_plans/*/; do
  stack=$(basename "$tests")
  [ "$stack" = __pycache__ ] && continue
  dir="$work/stacks/$stack"
  mkdir -p "$dir/tests"
  tar -C "$root/stacks/$stack" --exclude=.terraform -cf - . | tar -C "$dir" -xf -
  cp -R "$tests". "$dir/tests/"

  # Registry replicas lag a library release by a few minutes, as in
  # .github/actions/terraform-init.
  for attempt in 1 2 3 4 5; do
    if terraform -chdir="$dir" init -backend=false -input=false -no-color >"$dir/init.log" 2>&1; then
      break
    fi
    cat "$dir/init.log"
    if [ "$attempt" = 5 ] || ! grep -q 'Unresolvable module version constraint' "$dir/init.log"; then
      exit 1
    fi
    echo "the registry has not published the pinned module version yet; retrying in 30s"
    sleep 30
  done

  for rendered in "$work/rendered/$stack"/*.tfvars.json; do
    name=$(basename "$rendered" .tfvars.json)
    echo "== $stack/$name"
    terraform -chdir="$dir" test -no-color \
      -filter="tests/$name.tftest.hcl" -var-file="$rendered" || status=1
  done
done
exit "$status"
