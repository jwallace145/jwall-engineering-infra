# Costs

`cost report` prints monthly spend from AWS Cost Explorer, grouped by the
tags every resource this tool manages carries (see [Tags](../README.md#tags)).
Cost Explorer already aggregates the bill by tag, so there is no Cost and
Usage Report to parse and nothing to deploy: the command is read-only and
runs from the owner's shell.

```sh
jwall-engineering-sites cost report --project rusty-chess   # one project, by component
jwall-engineering-sites cost report                         # every project, then the untagged spend
jwall-engineering-sites cost report --months 6              # 1-13 calendar months, this one included (default 3)
```

## What it shows

**One project** (`--project`): the project's spend by its `Component` tag.
Task instances are all `Component=task-runner`, so task spend is split by
the `Workflow` tag, the calling workflow's name: what the tasks were for,
not only where they ran.

```text
rusty-chess by component  2026-08  2026-09  2026-10*
task-runner: Datagen            —   $22.00    $41.30
task-runner: SPRT          $18.20   $25.10    $30.40
task-runner: Train              —    $1.80    $14.60
container-instance          $9.10    $9.40     $9.20
dataset                     $0.20    $0.90     $2.10
------------------------  -------  -------  --------
total                      $27.50   $59.20    $97.60
  * estimated: the month is not closed yet
```

(Illustrative numbers.) A component's spend with no `Workflow` tag, such as
the task-runner's log group, is the bare component; spend tagged with the
project but no component is `(no component)`.

**The account** (no `--project`): spend by `Project`, with everything that
has no `Project` tag in one `(untagged)` row, then that untagged spend by
service and usage type: the top 15 rows, the rest summed. Untagged spend is
anything this tool does not manage (another project's resources), and the
charges AWS never tags: public IPv4 addresses, tax, and some data transfer.
It is reported, never dropped, so the totals match the bill.

Rows under a cent in every month are left out of the table but counted in
the total. Amounts are `UnblendedCost`, what the bill says, in USD. The
current month is marked `*`: Cost Explorer's figures for it are estimates
and lag by about a day.

## Credentials

Cost Explorer is account-wide: its permissions can't be limited to one
project, so no CI role is granted `ce:GetCostAndUsage` and the command
checks no role. Run it with the owner's credentials (it logs who it runs
as). Without the permission it exits 3. Each Cost Explorer request costs
$0.01; a report makes one (`--project`) or two.

## Cost allocation tags

A tag only appears in Cost Explorer once it is activated as a cost
allocation tag (Billing → Cost allocation tags), and then only for spend
after activation, unless a backfill is requested (up to 12 months back,
one a day). The tags the reports use, all active since 2026-10-30 with a
backfill from 2025-11-01:

| Tag | Used for |
|---|---|
| `Project` | the account report; the project filter |
| `Component` | the project report's rows |
| `Workflow` | task spend by workflow |
| `Purpose`, `Profile`, `Lifecycle`, `RunId`, `TaskId` | active for Cost Explorer and later reports (per-run cost) |

A new tag a component adds must be activated by the owner before it shows.
