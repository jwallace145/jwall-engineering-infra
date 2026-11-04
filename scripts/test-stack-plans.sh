#!/usr/bin/env bash
# Plans the bot stack against a mock AWS provider, with tfvars rendered by the
# CLI's own code for each case in tests/stack_plans. This proves the rendered
# inputs pass the stack's and the modules' validations; it touches no AWS.
#
# It works on a temporary copy, as the CLI does, so the test files never sit in
# stacks/bot: a real plan and the image's stacks do not see them.
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

tar -C "$root/stacks/bot" --exclude=.terraform -cf - . | tar -C "$work" -xf -
mkdir "$work/tests"
cp -R "$root"/tests/stack_plans/*.tftest.hcl "$root/tests/stack_plans/mocks" "$work/tests/"

(cd "$root" && uv run python -m tests.stack_plans.render "$work/rendered")

# Registry replicas lag a library release by a few minutes, as in
# .github/actions/terraform-init.
for attempt in 1 2 3 4 5; do
  if terraform -chdir="$work" init -backend=false -input=false -no-color >"$work/init.log" 2>&1; then
    break
  fi
  cat "$work/init.log"
  if [ "$attempt" = 5 ] || ! grep -q 'Unresolvable module version constraint' "$work/init.log"; then
    exit 1
  fi
  echo "the registry has not published the pinned module version yet; retrying in 30s"
  sleep 30
done

status=0
for rendered in "$work"/rendered/*.tfvars.json; do
  name=$(basename "$rendered" .tfvars.json)
  echo "== $name"
  terraform -chdir="$work" test -no-color \
    -filter="tests/$name.tftest.hcl" -var-file="$rendered" || status=1
done
exit "$status"
