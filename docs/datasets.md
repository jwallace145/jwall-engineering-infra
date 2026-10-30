# Datasets

A bot project can declare **datasets**: durable S3 buckets for data that has
to outlive the CI run that made it, such as NNUE self-play training data.
Every net the engine trains is built from it, and later generations are
compared against earlier ones, so it is kept forever.

What other storage doesn't do:
- **The task results bucket** keeps outputs for a week, and each instance
  can write only its own prefix.
- **GitHub artifacts** are the wrong place for gigabytes.

## The component

```yaml
components:
  # ...container-image, lichess-bot, task-runner...
  - kind: dataset
    name: nnue                                 # kebab-case; not records or tasks
    bucket: jwall-engineering-rusty-chess-nnue # <Organization>-<project>-<name>
    prefix: data/                              # everything the project writes lives under it
    writers: [task-runner]                     # optional: who may add objects
    # readers: [arn:aws:iam::123456789012:role/trainer]  # optional
    # transition_days: 0                       # optional: days before Intelligent-Tiering
```

| Field | Meaning |
|---|---|
| `name` | The dataset's name, in kebab-case. `records` and `tasks` are reserved, because the project already has those buckets. Names must be unique within the file. |
| `bucket` | Must be `<Organization>-<project>-<name>`. The tool derives the name and refuses a file that disagrees. The name is written out so the project's workflows can read it with `yq` without installing the tool. |
| `prefix` | Path segments ending in `/` with no leading `/`, for example `data/`. Writers and readers are granted this prefix only. |
| `writers` | Components whose roles may add objects under the prefix. Only `task-runner`, the task instances, is accepted today, and it needs a `task-runner` component. The default is none. |
| `readers` | IAM role ARNs granted read access under the prefix through the bucket policy. The default is none; the account's administrators can always read. |
| `transition_days` | Days before objects move to S3 Intelligent-Tiering. The default is 0, which means at the next daily lifecycle run. |

A project can declare several datasets, each with its own bucket.

## The bucket

The bucket is built by the library's
[`dataset-bucket`](https://registry.terraform.io/modules/jwallace145/jwall-engineering/aws/latest/submodules/dataset-bucket)
module, in the bot stack.

- **Kept forever.** There is no expiration rule, and Terraform refuses to
  destroy the bucket (`prevent_destroy`). Removing the component from the
  file therefore fails the plan instead of deleting data.
- **Write-once, by convention.** Writers add new keys; a new run gets a new
  partition and never overwrites. Versioning is off, because it would only
  store an accidental re-upload twice.
- **Intelligent-Tiering.** Writers upload in S3 Standard (the AWS CLI's
  default), and a lifecycle rule moves objects to Intelligent-Tiering.
  Objects under 128 KB, such as manifests, stay in Standard.
- **Hardened:**
  - SSE-S3 encryption with a bucket key;
  - all public access blocked;
  - `BucketOwnerEnforced`;
  - a bucket policy that denies requests without TLS;
  - incomplete multipart uploads aborted after 7 days.
- **Tagged** `Component = dataset` and `Purpose = dataset`, plus the six
  organisation tags.

## Who can do what

| Who | Write | Read | Delete |
|---|---|---|---|
| Task instances, when `writers: [task-runner]` | `PutObject` under the prefix (and `AbortMultipartUpload` for their own failed uploads) | `ListBucket` under the prefix only, never `GetObject` | no |
| A task profile's instances, through its `datasets` grant | `PutObject` under each `write` sub-prefix | `GetObject` under each `read` sub-prefix; `ListBucket` limited to those sub-prefixes | no |
| `readers` | no | `GetObject` and `ListBucket` under the prefix | no |
| The task launch role (`ci-task`) | no | no | no |
| The plan and apply roles (`ci-plan`, `ci-apply`) | the bucket's configuration only (`dataset/read`, `dataset/manage`) | no objects | no objects |
| The account's administrators | yes | yes | yes |

Task instances run pull request code, so they can only add data: nothing
automated can read it back or delete it. `PutObject` can still replace an
existing key. Every run writing under its own new `run=` partition is what
keeps that from happening, and versioning is off, so a replaced object
can't be recovered.

The instances write with the AWS CLI and the instance role's credentials:

```sh
aws s3 cp --only-show-errors FILE s3://jwall-engineering-rusty-chess-nnue/data/selfplay/v1/...
```

## Reading the data

Run these from your machine with the admin profile:

```sh
export AWS_PROFILE=prog-strength-admin
B=s3://jwall-engineering-rusty-chess-nnue/data

# Generations and runs
aws s3 ls "$B/selfplay/v1/"
aws s3 ls "$B/selfplay/v1/gen=1/"

# A run's size and object count
aws s3 ls --recursive --summarize "$B/selfplay/v1/gen=1/run=<run>/" | tail -2

# Download a run (or --exclude/--include to take only positions/)
aws s3 sync "$B/selfplay/v1/gen=1/run=<run>/" ./run=<run>/
```

Downloads from Intelligent-Tiering's frequent, infrequent and archive
instant access tiers are immediate. Only the optional archive tiers, which
aren't enabled here, would need a restore.

## Nets: `nets/`

Training jobs read the data and write nets through a GPU
[task profile](remote-tasks.md#task-profiles-gpu-tasks). Its grant on the
dataset names sub-prefixes of the dataset's `prefix`:

```yaml
profiles:
  gpu:
    datasets:
      nnue: { read: [selfplay/], write: [nets/] }
```

So for rusty-chess:

| Prefix | Cpu task instances | Gpu task instances |
|---|---|---|
| `data/selfplay/` | add | read |
| `data/nets/` | add | add |
| anything else under `data/` | add | nothing |

Each trained net lives under its own ID and is never overwritten:

```text
data/nets/<net id>/net.nnue
data/nets/<net id>/training.json   # data selection, λ, K, epochs, losses, GPU, image, commit
data/nets/<net id>/log.txt
data/nets/<net id>/checkpoints/    # for resuming an interrupted run
```

Nothing automated deletes, and a new run uses a new net ID, so earlier nets
stay.

## Cost

Compressed positions plus full game records come to about 18 bytes per
position. So 100 million positions are about 1.8 GB, and a billion about
18 GB.

| Storage tier | $/GB-month |
|---|---|
| S3 Standard | about $0.023 |
| Intelligent-Tiering, after 30 days without access | about $0.0125 |
| Intelligent-Tiering, after 90 days without access | about $0.004 |

- **Storage:** an 18 GB billion-position dataset costs well under a dollar a
  month.
- **Monitoring:** Intelligent-Tiering charges $0.0025 per 1,000 objects a
  month, which is negligible for shards.
- **Transitions:** the lifecycle transition costs $0.01 per 1,000 objects.
- **Uploads:** `PutObject` costs $0.005 per 1,000.
- **Free:** uploads from the instances (data transfer in, same region).
- **Downloads:** to your machine, about $0.09 per GB out to the internet.

## Adding a dataset

1. Add the component to the project's `jwall-engineering-sites.yaml`.
2. Add the bucket to `local.bot_datasets` in `foundation/bots.tf` and
   release. That gives the project's plan and apply roles `dataset/read` and
   `dataset/manage` for the bucket.
3. Run **Bot apply** for the project. It creates the bucket and the
   writers' policies.
