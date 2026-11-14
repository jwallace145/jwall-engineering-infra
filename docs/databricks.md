# Databricks workspaces: the `databricks-workspace` component

A repository whose only component is a `databricks-workspace` is a **Databricks
workspace project**. This repository provisions a governed workspace for it on
the owner's Databricks-on-AWS account: a customer-managed VPC with private
subnets and a NAT instance, a workspace, its own Unity Catalog catalog on a
dedicated bucket, central compute policies, and an event log of every run.
The project repository holds one file and no Terraform; the infrastructure is
planned and applied from here, like a bot.

The first consumer is `databricks-poc`. Nothing here has run against a real
Databricks account yet: it is written from the code and the tests, and the
parts the first live apply must confirm are listed under
[Known gaps](#known-gaps).

Contents: [setup](#one-time-setup), [onboarding a project](#onboarding-a-project),
[the project file](#the-project-file), [plan](#what-plan-shows),
[apply](#what-apply-does), [destroy](#destroy), [doctor](#doctor),
[layout](#bucket-and-prefix-layout), [tags](#tags-on-every-resource),
[compute policies](#central-compute-policies), [costs](#costs),
[known gaps](#known-gaps), [runbook](#runbook).

## One-time setup

The owner does these once, before anything is enabled. Until
`databricks.enabled: true` in `config.yaml`, the foundation creates no
Databricks resource and every `live`, `between` and `after` check SKIPs with a
pointer back here.

1. **Confirm the account.** It must be a full Databricks-on-AWS account on the
   Premium plan or above, with the account console at
   `accounts.cloud.databricks.com`. A trial or a workspace-only login has no
   account console and cannot create customer-managed networks or a metastore.
2. **Create the service principal.** In the account console, create one
   service principal for Terraform and the CLI, give it the **account admin**
   role directly (admin through a group is not detected by `DBX-LIVE-003`),
   and create an OAuth secret for it.
3. **Fill in `config.yaml`** under `databricks`: replace the nil-UUID
   placeholders for `account_id` and `client_id` with the account ID and the
   service principal's application (client) ID. Neither is a secret. Replace
   the `owner@example.com` placeholders with real emails in
   `databricks.metastore_admins` and in each project's
   `projects.<name>.databricks.admins` and `users`. Every member must also
   exist as an account user in the console (`DBX-LIVE-005`).
4. **Store the secret.** From the owner's shell with AWS credentials:

   ```sh
   jwall-engineering-sites --file jwall-engineering-sites.yaml databricks credentials set
   jwall-engineering-sites --file jwall-engineering-sites.yaml databricks credentials check
   ```

   `set` prompts without echo and writes a SecureString at
   `/jwall-engineering/databricks/client-secret` (`databricks.secret_parameter`).
   `check` exits 0 if it exists and decrypts and 3 if not; it never prints the
   value. The secret reaches Terraform only through the child process's
   environment and the SDK clients in memory, never a variable, a file, state,
   a log, a plan comment or an event.
5. **Delete a pre-existing us-east-1 metastore, if there is one.** Databricks
   creates a metastore for a region automatically with the first workspace
   there, and an account holds one metastore per region. The foundation
   creates its own (`jwall-engineering-us-east-1`), so an old one must go
   first. This is manual and **only with the owner's go-ahead, right before**:
   in the account console, open Catalog, then Metastores, open the us-east-1
   metastore, unassign every workspace, and delete it. `DBX-LIVE-004`
   names the one it found. If you would rather keep it, set
   `databricks.metastore_name` to its name instead.
6. **Set `databricks.enabled: true`** in a pull request. The next foundation
   plan creates the metastore (owned by an account group holding the service
   principal and the metastore admins), audit log delivery to
   `jwall-engineering-databricks-audit-logs`, and the CI roles' access to the
   secret and the event log.
7. **Deployment-name prefix (optional).** `databricks.deployment_name_prefix`
   is `null`, so Databricks picks each workspace URL. Databricks only accepts
   a deployment name once it has configured a prefix for the account. If it
   has, set the prefix here: each workspace is then created with the
   deployment name `<project>` (or the component's `deployment_name`) and its
   URL starts `<prefix>-<name>`. The name is passed to the provider
   explicitly because the provider does not compute it. Changing it later
   **replaces the workspace**.

## Onboarding a project

In this order:

1. In `config.yaml`, add `projects.<name>.databricks` with `compute_policies`
   (a subset of `databricks.compute_policies`), `cost_center`, a `vpc_cidr`
   (a /16 to /20, no overlap with another project or the bot VPC `10.0.0.0/16`),
   `admins` (at least one email) and `users`.
2. In `foundation/databricks.tf`, add the project to `local.databricks_projects`
   with its repository and repository ID. The release that applies it creates
   the project's `jwall-engineering-<name>-ci-plan` and `-ci-apply` roles
   (`compute/*` and `databricks/*` policies; no release or task role). A
   Databricks project must not also be a site or a bot.
3. Commit `jwall-engineering-sites.yaml` in the project repository (below).
4. Open a pull request here. The `Databricks plan` job posts one comment for
   both stacks.
5. After the owner reads it, run the **Databricks apply** workflow with the
   project name (`workflow_dispatch`, in the `prod` environment).

The project name must be at most 21 characters:
`<organization>-<project>-databricks-cross-account` must fit IAM's 64
characters (`DBX-CFG-004`).

## The project file

```yaml
version: 1
project: databricks-poc
repository: example-org/databricks-poc

tags:
  Organization: jwall-engineering
  Project: databricks-poc
  Environment: prod
  Repository: example-org/databricks-poc
  Owner: owner@example.com

components:
  - kind: databricks-workspace
    compute_policies: [small-interactive, jobs]
    # deployment_name: databricks-poc   # optional; needs databricks.deployment_name_prefix
```

The file names only what the project asks for. The network range, cost center
and members are central (`projects.<name>.databricks` in `config.yaml`); a
project cannot widen a compute policy, add a node type or pick its own range.
`compute_policies` must be kebab-case, must not repeat, and must be a subset
of the project's approved list. The CLI adds `ManagedBy`, `CostCenter` and
`Workspace` to the tags itself. The state is `project=<name>/env=prod/` in the
shared bucket, one file per stack:
`<name>-prod-account.tfstate` and `<name>-prod-workspace.tfstate`.

## What `plan` shows

`plan` runs the config checks, then the live checks, then plans the account
stack and, when it can, the workspace stack, and writes one comment for both
(`plan-comment.md`, `plan.txt`, `changes=true|false`).

- **Before the workspace exists** (the first pull request for a project) the
  account stack has no `workspace_url` output, so the workspace stack cannot
  be planned. The comment says *the workspace stack is planned after the
  workspace exists* and shows only the account plan. It never fails on the
  missing outputs.
- **Stale marker.** If the account plan replaces or destroys the workspace
  (`module.workspace.databricks_mws_workspaces.this`), the workspace plan is
  against a workspace that will not exist, so the comment marks it
  **stale**; apply plans the workspace stack again against the new workspace.
- If the account plan fails, the workspace stack is reported as not planned.
  A failed check stops the plan before Terraform, with exit code 3.

A merge does not apply anything. Applying is the **Databricks apply**
workflow, by hand.

## What `apply` does

`apply` runs these stages in order. Each writes a started and a success,
skipped or failure [event](#event-log), and the first failed `error` check
stops the run with exit code 3.

1. **Config checks** (offline): `DBX-CFG-*`.
2. **Live checks** (read-only calls to AWS and the Databricks account):
   `DBX-LIVE-*`.
3. **Account stack**: plan, and apply only if there are changes. It creates the
   VPC, NAT instance, S3 gateway endpoint, root bucket, cross-account role,
   the Databricks network, credential and storage configurations, the
   workspace, the metastore assignment, and the workspace's account groups
   `<project>-workspace-admins` and `<project>-workspace-users`.
4. **Readiness wait**: `DBX-BTW-*`. Polls every 15 seconds, each check for up
   to `databricks.readiness_timeout` (20 minutes), until the workspace is
   `RUNNING`, the workspace URL answers an authenticated call (connection
   errors, 401/403, 429 and 5xx are retried), and the service principal is a
   workspace admin (the assignment is listed and an admin-only read works);
   then checks once that the metastore is assigned. A timeout fails with the
   last state seen.
5. **Workspace stack**: planned again with the account's outputs, and applied.
   It creates the catalog bucket, the Unity Catalog role, the storage
   credential, the external location, the catalog (isolated, bound to this
   workspace only), the compute policies and the workspace settings: personal
   access tokens are capped at 90 days, and the built-in `users` group loses
   unrestricted cluster creation. Only the governance project
   (`databricks.governance_project`) also enables the system schemas.
6. **After checks**: `DBX-AFT-*`.
7. **Events and outputs**: `outputs.json` (both stacks, merged) and the job
   summary.

### Partial failure and re-running

If the account stack succeeds and the workspace stack fails, the run says
*account stack applied; workspace stack failed; re-running apply is safe*. Each
step only does what is left, so re-running `apply` converges with no manual
step. The same holds when a `between` check fails after the account stack:
the summary notes the account stack is applied and a re-run is safe. A
Terraform failure is matched against the failure catalogue
([runbook](#runbook)); the job summary and `failure.md` give the class, the
likely cause, the next steps and the raw error, with the secret masked. An
error the catalogue does not know still names the stack, the resource address
and the debug log.

## Destroy

`destroy --yes` (the **Databricks apply** workflow with `destroy: true`) first
prints what it deletes and refuses without `--yes`. The order matters:

1. **Empty the catalog bucket**: every object version and delete marker, in
   batches, while the bucket policy still lets the apply role list and delete
   (and never read or write) objects.
2. **Destroy the workspace stack**: the catalog, its storage credential and
   external location, the catalog bucket, the Unity Catalog role, the compute
   policies and the settings. If the account stack no longer has a workspace
   and the workspace state is empty, this step is skipped; if the state still
   holds resources, destroy stops and says so (see [account-state](#account-state)).
3. **Destroy the account stack**: the workspace, VPC, subnets, endpoints,
   security group, NAT instance, root bucket and its contents, the roles, the
   account groups and the metastore assignment.

It deletes the workspace and everything in it (clusters, notebooks, jobs) and
the catalog's data. It keeps the metastore, the event log and the client
secret in SSM. Every step is idempotent: re-running a failed destroy continues
where it stopped.

## Doctor

```sh
jwall-engineering-sites --file jwall-engineering-sites.yaml databricks doctor [--stage config|live|between|after]
```

`doctor` runs the same read-only checks against the live project and prints the
table; `--stage` is repeatable and the default is all four. It needs the
project's plan role. It reports what is broken instead of aborting: if the
credential cannot be loaded it reports `DBX-DOC-002` and skips the live,
between and after stages; if the account stack's state cannot be read it
reports `DBX-DOC-001` and skips between and after; if the account stack has no
outputs yet it skips those stages with a notice. `checks.json` is written
either way. The exit code is 3 if any `error` check failed; warnings do not
fail it. While `databricks.enabled` is false, the live checks SKIP.

## Bucket and prefix layout

| Bucket | Prefix | Holds |
| --- | --- | --- |
| `jwall-engineering-<project>-databricks-root` | Databricks' own | The workspace root storage. Private, S3-managed encryption; its policy grants only the access Databricks documents. |
| `jwall-engineering-<project>-databricks-catalog` | `managed/` | The catalog's Unity Catalog managed storage (`s3://.../managed/`). Not a DBFS root; reachable through Unity Catalog only. |
| `jwall-engineering-databricks-audit-logs` | `audit/` | Databricks audit log delivery (foundation, one for the account). |
| `jwall-engineering-databricks-audit-logs` | `events/project=<p>/date=YYYY-MM-DD/` | The CLI's event records. |

Other names derived from the project: the roles
`jwall-engineering-<project>-databricks-cross-account` and `-uc`, and the
catalog `<project>` with hyphens replaced by underscores. The catalog bucket's
policy denies every principal except the Unity Catalog role (object access),
the CI and admin roles (manage the bucket, never read or put objects) and the
apply role (additionally delete and list, so `destroy` can empty it).

### Event log

Every `validate`, `plan`, `apply`, `destroy` and `outputs` run appends one JSON
record per stage (`checks-config`, `checks-live`, `account-plan`,
`account-apply`, `readiness`, `workspace-plan`, `workspace-apply`, `verify`,
`empty-catalog-bucket`, `workspace-destroy`, `account-destroy`) under
`events/`. A record holds the event, time, run ID, project, stage, result
(started, success, skipped or failure), duration, CLI version, repository,
commit, actor, workspace ID, state key, the failure class and the IDs of failed
checks. Never secrets, plan bodies or raw errors. Keys are unique and written
with no overwrite. If the bucket is unreachable the write warns and the run
continues.

## Tags on every resource

Every AWS resource carries `Organization`, `Project`, `Environment`,
`Repository`, `ManagedBy` and `Owner` through `default_tags`, plus
`CostCenter` (from `config.yaml`) and `Workspace` (`<project>-prod`). The
workspace and its clusters carry the same keys: the workspace's `custom_tags`
use them, and each compute policy pins them as fixed `custom_tags`, so cluster
instances and `system.billing.usage` records can be traced to a project.

`DBX-CFG-005` requires non-empty `Project`, `Environment`, `CostCenter`,
`Owner`, `ManagedBy` and `Workspace` in the effective tags, and refuses the
keys Databricks reserves (`Vendor`, `Creator`, `ClusterName`, `ClusterId`,
`Name`) in the project file's `tags`. The catalog carries `Project` and
`Owner`.

## Central compute policies

Policies are defined once, centrally, under `databricks.compute_policies` in
`config.yaml`, each as a Databricks policy `definition`. A project lists the
names it gets in its approved `compute_policies`, and only those are installed
in its workspace, with `CAN_USE` for `<project>-workspace-users`. Users get no
unrestricted cluster creation.

```yaml
databricks:
  compute_policies:
    jobs:
      description: Job clusters, up to four workers
      definition:
        cluster_type: {type: fixed, value: job}
        num_workers: {type: range, maxValue: 4}
        autotermination_minutes: {type: fixed, value: 30, hidden: true}
        node_type_id: {type: allowlist, values: [m7g.large, m7g.xlarge]}
        aws_attributes.availability: {type: fixed, value: SPOT_WITH_FALLBACK}
```

**Every policy must set `autotermination_minutes`**, so an idle cluster stops
costing. `node_type_id` must be an allowlist (or fixed) of at least one node
type, and any default must be in it. A policy must not set `custom_tags.*`;
the project's tags are added for it. A change to a policy is a pull request
here, reviewed like any guardrail.

## Costs

Rough monthly figures in us-east-1; they do not include Databricks usage, and
no sweeper or budget enforces them.

| Item | Cost |
| --- | --- |
| NAT instance (`t4g.nano`, 8 GB volume, its public IPv4) | about $3 per month per workspace, always on |
| S3 (root and catalog buckets, audit logs, events) | storage and requests only; small until data lands |
| Cluster EC2 | on demand or spot, per policy; zero when no cluster runs |
| DBUs | per policy: the node types and `num_workers` in the policy bound a cluster, and every cluster terminates after its `autotermination_minutes` |

There is no NAT gateway and no Elastic IP. The S3 gateway endpoint is free and
keeps same-region S3 traffic off the NAT. The NAT instance is a single
instance with limited, burstable network throughput; it can be replaced by a
larger Arm instance type if egress needs grow.

## Known gaps

- **Serverless is outside the policies.** Serverless compute stays enabled.
  Compute policies and the S3 endpoint policy do not cover it, so claims of
  strict cost control and of routing only to approved data hold for classic
  compute only. Serverless network and budget policies are future work.
- **Privileged roles can rewrite the catalog bucket policy and the Unity
  Catalog trust.** The CI apply role and Databricks account admins can change
  the catalog bucket's policy or the role's trust policy. The policies deny
  object reads and writes to them, but not their own reconfiguration.
- **To confirm on the first live apply:**
  - metastore auto-assignment: whether Databricks assigns the new workspace to
    the region's metastore itself, so `databricks_metastore_assignment` fails
    "already assigned" (see [DBX-METASTORE-ALREADY-ASSIGNED](#dbx-metastore-already-assigned));
  - `deployment_name`: that a second account plan shows no diff, and what
    the prefix does for this account;
  - that workspace-stack destroy succeeds (workspace settings, system schemas);
  - that the `ec2:VpceServiceName` condition is honoured for the gateway
    endpoint.

## Runbook

Every failed check and every catalogued Terraform failure links to a heading
below. Check IDs use these prefixes: `DBX-CFG` (config, offline), `DBX-LIVE`
(live), `DBX-BTW` (between, after the account stack), `DBX-AFT` (after, once
the workspace stack is applied) and `DBX-DOC` (found by `doctor`). A failed
`error` check exits 3; a `warning` check never blocks.

### Network CIDRs

`DBX-CFG-001`, `DBX-CFG-002`. The private subnets (`cidrsubnet(vpc, 4, 8)` and
`cidrsubnet(vpc, 4, 9)`) must be between /17 and /26 and not overlap the
public subnets, and the VPC range must overlap no other Databricks project
and not the bot VPC. Change `projects.<name>.databricks.vpc_cidr` in
`config.yaml` to a free /16 to /20 range.

### Compute policies

`DBX-CFG-003`, `DBX-AFT-003`. CFG-003: a listed policy is not defined, lacks
`autotermination_minutes`, sets `custom_tags.*`, or has no `node_type_id`
allowlist or a default outside it: fix it under `databricks.compute_policies`
in `config.yaml`. AFT-003: a policy is missing from the workspace or lacks
the project's fixed tags; re-run `apply`, which reinstalls it.

### Resource names

`DBX-CFG-004`. A derived bucket or role name exceeds S3's 63 or IAM's 64
characters, or the deployment name is invalid. Shorten the project name (21
characters at most), or set a shorter `deployment_name` on the component or
`databricks.deployment_name_prefix`.

### Required tags

`DBX-CFG-005`. The project file states a key Databricks reserves, or a
required tag is missing from the effective tags. Edit `tags` in the project
file as the message says. See [Tags on every resource](#tags-on-every-resource).

### Credentials

`DBX-LIVE-001`, `DBX-LIVE-002`, `DBX-DOC-002`. LIVE-001: the secret is not at
`databricks.secret_parameter` or the role cannot read it; run
`databricks credentials set` as the owner. LIVE-002: Databricks refused the
OAuth token, or `account_id`/`client_id` are still the nil UUID; check both in
`config.yaml` and that the SSM secret is the current one (the failure shows
only the exception type, never its text). DOC-002: `doctor` could not load the
secret, so it skipped the live, between and after stages; fix as LIVE-001, then
`databricks credentials check`.

### Account admin

`DBX-LIVE-003`. No service principal with `databricks.client_id` exists in the
account, or it lacks the `account_admin` role. Grant the role to the principal
directly in the account console; admin through a group is not detected.

### Metastore

`DBX-LIVE-004`. us-east-1 has no metastore named `databricks.metastore_name`.
If it names an automatically created one, delete that one by hand with the
owner's go-ahead ([setup step 5](#one-time-setup)), or set
`databricks.metastore_name` to its name. If there is none, apply the
foundation first (it creates the metastore once `databricks.enabled` is true).

### Workspace members

`DBX-LIVE-005`. A member email in `projects.<name>.databricks.admins` or
`users` is a placeholder (`@example.com`) or is not an account user. Replace
it with a real email, or add the user in the account console.

### Bucket names

`DBX-LIVE-006`. A derived bucket name is owned by another AWS account, or the
role cannot read it (check the `databricks/read` policy). Choose a different
project name, since the bucket names derive from it.

### Instance availability

`DBX-LIVE-007` (warning). A node type in a policy is not offered in one of the
two zones. Remove it from `node_type_id` in the policy, or clusters in that
zone fail to start.

### vCPU quota

`DBX-LIVE-008` (warning). The largest policy (`num_workers` plus the driver,
at the largest node type) can need more vCPUs than the account's *Running
On-Demand Standard instances* quota. Request an increase, or lower
`num_workers` in the policy. It compares the quota with the policy, not with
current usage.

### NAT AMI

`DBX-LIVE-009`. `databricks.nat_ami` matches no image or several, or the
image is not arm64. Set it to a current Amazon Linux 2023 arm64 image name.
Changing it replaces the NAT instance.

### Deployment name

`DBX-LIVE-010`. `<prefix>-<name>` already belongs to another workspace in the
account. Set a different `deployment_name` on the component. It skips when no
prefix is set or the workspace is already ours.

### Workspace readiness

`DBX-BTW-001`, `DBX-BTW-002`. The workspace does not exist, is in a terminal
state (the message gives Databricks' status text), was still provisioning
after `databricks.readiness_timeout`, or its URL did not answer an
authenticated call. The account stack stays applied. Fix the cause named,
raise `readiness_timeout` if provisioning is just slow, and re-run `apply`; it
waits again. See also [DBX-WORKSPACE-FAILED](#dbx-workspace-failed).

### Workspace admin

`DBX-BTW-003`. The group `<project>-workspace-admins` does not hold ADMIN on
the workspace, or does but the service principal cannot yet read admin
settings (the assignment is not yet effective). Re-run `apply`; the account
stack assigns the group after the metastore assignment.

### Metastore assignment

`DBX-BTW-004`. The workspace has no metastore assignment, or it points at a
different metastore than the foundation's. Re-run `apply`, or unassign the
other metastore in the account console first.

### Catalog

`DBX-AFT-001`, `DBX-AFT-002`. AFT-001: the storage credential cannot read,
list, write or delete under `s3://<catalog bucket>/managed/`; check the
Unity Catalog role's trust and policy and the bucket policy, then re-run
`apply` (IAM can take a minute to propagate). AFT-002: the catalog is
missing, its storage root differs, it is not `ISOLATED`, or it is bound to
other workspaces. Re-run `apply` to restore it; never change these in the
console.

### Egress

`DBX-AFT-004` (warning). The private route table is not found, the S3
gateway endpoint is not on it, or its default route is missing, goes to an
internet gateway, or does not target the running NAT instance's interface.
Re-run `apply`; if the NAT instance was replaced, apply re-points the route.

### Account state

`DBX-DOC-001`. `doctor` could not read the account stack's state, so it
skipped the between and after stages. Check that the project's `ci-plan` role
can read the state bucket, and run `plan` to see Terraform's own error. The
same section applies when `destroy` says the workspace stack still holds
resources but the account stack has no `workspace_url`: re-apply the account
stack so the workspace stack can be reached, or remove the remaining
resources by hand, then re-run `destroy`.

### DBX-AUTH

Databricks rejected the service principal credentials. Check
`/jwall-engineering/databricks/client-secret` holds the current secret and has
not expired, and that the service principal is an account admin. Re-run
`apply`; no partial state is lost.

### DBX-CRED-VALIDATION

Databricks rejected the cross-account role. Compare the role's policy with
the checks named in the error, and confirm its trust policy names the
Databricks account ID as the external ID. If the role was just created, wait
a minute and re-run `apply`.

### DBX-STORAGE-VALIDATION

Databricks cannot use the root bucket. The bucket policy must grant the
cross-account role List, Put, Delete and GetBucketLocation, and the bucket
must be in us-east-1. Re-run `apply` once the policy is in place.

### DBX-NETWORK-VALIDATION

Databricks rejected the VPC configuration: subnets in two availability zones,
a self-referencing security group, outbound access. Compare the check names in
the error with the VPC the stack created; confirm the subnets span two zones
and the security group allows all TCP and UDP traffic within itself.

### DBX-WORKSPACE-FAILED

The workspace entered `FAILED`: Databricks accepted a credential, storage or
network configuration it cannot use. Fix the cause named and re-run `apply`.
The provider deletes the failed workspace itself; only if the error also
reports a delete failure, remove it by hand in the account console first.

### DBX-NO-EGRESS

Cluster nodes cannot reach the control plane, so the secure cluster
connectivity tunnel never came up. Check the NAT instance is running and the
private route tables point at it, and that the security group allows outbound
443 and the relay port. Re-run `apply` once egress works.

### AWS-IAM-PROPAGATION

A new IAM role was not visible yet (`MalformedPolicy: Invalid principal`). IAM
is eventually consistent. Re-run `apply`; it resumes where it stopped, and this
is the one class marked safe to retry blindly. If it repeats, check the
principal ARN in the policy exists.

### DBX-METASTORE-ALREADY-ASSIGNED

Databricks auto-assigned the new workspace to the region's metastore, so
Terraform's own assignment collides. Import the existing assignment
(`terraform import databricks_metastore_assignment.this <workspace_id>|<metastore_id>`),
or disable metastore auto-assignment for the account, then re-run `apply`. If
this happens on the first live apply, decide between importing and removing
the resource from the stack.

### DBX-METASTORE-ASSIGNMENT

The metastore could not be assigned: the service principal is not a metastore
admin, or the metastore is in another region. Make the service principal a
metastore admin, check the metastore and workspace share a region, and
re-run `apply`.

### TF-ALREADY-EXISTS

A resource already exists outside Terraform state: a previous run created it
and state was not saved, or it was created by hand. Confirm it belongs to this
project, import it with `terraform import <address> <id>` (the address is in
the error), and never delete a resource this stack does not own; ask the
owner.
