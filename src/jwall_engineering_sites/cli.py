"""The `jwall-engineering-sites` command line.

Every command loads config.yaml (baked into the image) and the site file
(from the working directory), derives the rest, runs its preflight checks and
then Terraform or S3. Failures map to exit codes: 1 Terraform/AWS, 2
configuration, 3 preflight.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
from pathlib import Path
from typing import Annotated

import boto3
import typer

from . import aws, github, image, releases, report, rollout, stack, sync, tasks
from . import bot as botlib
from .components import CPU_PROFILE, TaskRunner
from .errors import ConfigError, PreflightError, RunError, SitesError
from .log import annotate_error, artifacts_dir, phase, setup_logging
from .project import Project, Role
from .settings import Settings, default_config_path, load_settings, parse_duration
from .site import DEV_PREFIX, PROD_PREFIX, SITE_FILE, Site, load_site_config

app = typer.Typer(
    name="jwall-engineering-sites",
    help="Provisions and publishes a jwall.engineering site from jwall-engineering-sites.yaml.",
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode=None,
    pretty_exceptions_enable=False,
)
preview_app = typer.Typer(
    help="Pull request previews under previews/pr-<n>/.", no_args_is_help=True
)
config_app = typer.Typer(help="The tool configuration.", no_args_is_help=True)
report_app = typer.Typer(
    help="Render the Markdown the workflows post, from saved output.", no_args_is_help=True
)
bot_app = typer.Typer(
    help="Bot projects: the container image and the Lichess bot server.", no_args_is_help=True
)
secrets_app = typer.Typer(
    help="The bot's secrets in SSM Parameter Store (values are never printed).",
    no_args_is_help=True,
)
task_app = typer.Typer(
    help="Remote CI tasks on ephemeral, on-demand EC2 instances (bot projects with a task-runner).",
    no_args_is_help=True,
)
bot_app.add_typer(secrets_app, name="secrets")
app.add_typer(task_app, name="task")
app.add_typer(bot_app, name="bot")
app.add_typer(preview_app, name="preview")
app.add_typer(config_app, name="config")
app.add_typer(report_app, name="report")

LOG_FILE = "jwall-engineering-sites.log"


def tool_version() -> str:
    try:
        return package_version("jwall-engineering-sites")
    except PackageNotFoundError:
        return "0.0.0+local"


@dataclass
class Run:
    settings: Settings
    site_file: Path
    artifacts: Path
    logger: logging.Logger
    _site: Site | None = None
    _project: Project | None = None

    @property
    def project(self) -> Project:
        """The project file of either kind. Kind-aware commands use this."""
        if self._project is None:
            self._project = Project.from_config(load_site_config(self.site_file), self.settings)
            self.logger.debug(
                "project %s (%s), state key %s",
                self._project.project,
                self._project.kind,
                self._project.state_key,
            )
        return self._project

    @property
    def site(self) -> Site:
        if self._site is None:
            self._site = Site.from_config(load_site_config(self.site_file), self.settings)
            self.logger.debug(
                "site %s: bucket %s, state key %s",
                self._site.project,
                self._site.bucket,
                self._site.state_key,
            )
        return self._site


@app.callback()
def main(
    ctx: typer.Context,
    file: Annotated[Path, typer.Option("--file", "-f", help="Path to the site file.")] = SITE_FILE,
    config: Annotated[Path | None, typer.Option("--config", help="Path to config.yaml.")] = None,
    debug: Annotated[bool, typer.Option("--debug", help="Console log at debug level.")] = False,
) -> None:
    try:
        artifacts = artifacts_dir()
        settings = load_settings(config)
    except SitesError as exc:
        # Logging is not configured yet: report plainly.
        print(f"error: {exc}", file=sys.stderr)
        raise typer.Exit(exc.exit_code) from exc
    level = "debug" if debug else settings.tool.log_level
    logger = setup_logging(level, settings.tool.log_format, artifacts / LOG_FILE)
    logger.debug(
        "jwall-engineering-sites %s, config %s", tool_version(), config or default_config_path()
    )
    ctx.obj = Run(settings=settings, site_file=file, artifacts=artifacts, logger=logger)


def _command(body: Callable[[Run], None]) -> Callable[[typer.Context], None]:
    """Run a command body, mapping SitesError to an exit code and an annotation."""

    def wrapper(ctx: typer.Context) -> None:
        run: Run = ctx.obj
        try:
            body(run)
        except SitesError as exc:
            run.logger.error("%s", exc)
            annotate_error(str(exc))
            raise typer.Exit(exc.exit_code) from exc

    return wrapper


# -- helpers ------------------------------------------------------------------


def _terraform(run: Run) -> stack.Terraform:
    """Terraform for the project's stack: the site stack for a site (exactly as
    before) or the bot stack for a bot project."""
    target: stack.Target = run.site if run.project.kind == "site" else run.project
    return stack.Terraform(target, run.settings, run.logger)


def _preflight(run: Run, role: Role, prefix: str | None) -> None:
    with phase("preflight"):
        project = run.project
        session = boto3.session.Session(region_name=project.region)
        aws.check_role(session.client("sts"), project.role_name(role), run.logger)
        if project.kind == "bot":
            run.logger.info("bot %s: state %s", project.project, project.state_key)
            return
        site = run.site
        if prefix is not None:
            _check_site_bucket(session, site, prefix, run)
        run.logger.info("site %s: bucket %s, state %s", site.project, site.bucket, site.state_key)


def _check_site_bucket(session: boto3.session.Session, site: Site, prefix: str, run: Run) -> None:
    """ListBucket under the prefix the command will use. Roles whose ListBucket
    is conditioned on a prefix (preview, deploy) can still pass this."""
    s3 = session.client("s3")
    try:
        s3.list_objects_v2(Bucket=site.bucket, Prefix=prefix, MaxKeys=1)
    except Exception as exc:
        raise PreflightError(f"cannot list s3://{site.bucket}/{prefix}: {exc}") from exc


def _dist_path(run: Run, dist: Path | None) -> Path:
    path = dist if dist is not None else Path(run.site.site_section.output_dir)
    if not path.is_dir():
        raise PreflightError(f"built site not found at {path}; pass --dist or build first")
    return path


def _sync_to(
    run: Run, s3: boto3.session.Session, prefix: str, dist: Path, cache_control: str
) -> sync.SyncResult:
    site = run.site
    client = s3.client("s3")
    local = sync.scan_local(
        dist,
        cache_control=cache_control,
        immutable_cache_control=run.settings.sync.immutable_cache_control,
        immutable_paths=site.site_section.immutable_paths,
        html_cache_control=run.settings.sync.html_cache_control,
    )
    remote = sync.list_remote(client, site.bucket, prefix)
    plan = sync.plan_sync(prefix, local, remote)
    run.logger.info(
        "sync plan for s3://%s/%s: %d to upload, %d unchanged, %d to delete",
        site.bucket,
        prefix,
        len(plan.upload),
        len(plan.skip),
        len(plan.delete),
    )
    return sync.execute_sync(client, site.bucket, plan, run.logger)


def _with_terraform(run: Run, body: Callable[[stack.Terraform], None]) -> None:
    terraform = _terraform(run)
    failed = True
    try:
        terraform.prepare()
        body(terraform)
        failed = False
    finally:
        terraform.cleanup(failed=failed)


# -- commands -----------------------------------------------------------------


@app.command()
def validate(ctx: typer.Context) -> None:
    """Validate config.yaml and the project file, then `terraform validate` offline."""

    def body(run: Run) -> None:
        project = run.project
        with phase("validate"):
            if project.kind == "site":
                site = run.site
                run.logger.info("site file ok: project %s, domain %s", site.project, site.domain)
                run.logger.info("derived: bucket %s, state key %s", site.bucket, site.state_key)
            else:
                run.logger.info(
                    "project file ok: project %s, kind bot, image %s, instance %s",
                    project.project,
                    project.image_repository,
                    project.bot_name,
                )
                run.logger.info("derived: state key %s", project.state_key)

            def tf(terraform: stack.Terraform) -> None:
                terraform.init(backend=False)
                terraform.validate()

            _with_terraform(run, tf)
            run.logger.info("stack validates")

    _command(body)(ctx)


@app.command()
def plan(ctx: typer.Context) -> None:
    """Plan the site's infrastructure and write the plan comment."""

    def body(run: Run) -> None:
        _preflight(run, "plan", prefix="")
        exit_code = stack.PLAN_ERROR
        plan_text = ""

        def tf(terraform: stack.Terraform) -> None:
            nonlocal exit_code, plan_text
            with phase("init"):
                terraform.init()
            with phase("plan"):
                result = terraform.plan(detailed_exitcode=True)
                exit_code, plan_text = result.returncode, result.output

        try:
            _with_terraform(run, tf)
        finally:
            (run.artifacts / "plan.txt").write_text(plan_text)
            comment = report.plan_comment(
                exit_code,
                plan_text,
                run.settings.plan.comment_max_bytes,
                run.project.config.domain or run.project.project,
                github.provenance(tool_version()),
                applied_by="Bot apply" if run.project.kind == "bot" else None,
            )
            (run.artifacts / "plan-comment.md").write_text(comment)
            github.write_output("result", report.plan_result_name(exit_code))
            github.write_output("changes", "true" if exit_code == stack.PLAN_CHANGES else "false")
        github.append_summary(
            report.run_summary(
                f"Terraform plan: {report.plan_result_name(exit_code)}",
                {"project": run.project.project, "tool": tool_version()},
            )
        )

    _command(body)(ctx)


@preview_app.command("deploy")
def preview_deploy(
    ctx: typer.Context,
    pr: Annotated[int, typer.Option("--pr", min=1, help="Pull request number.")],
    dist: Annotated[Path | None, typer.Option("--dist", help="Built site directory.")] = None,
) -> None:
    """Publish a built site to previews/pr-<n>/."""

    def body(run: Run) -> None:
        site = run.site
        if not site.config.preview.enabled:
            raise PreflightError("previews are disabled for this site (preview.enabled: false)")
        prefix = site.preview_prefix(pr)
        path = _dist_path(run, dist)
        _preflight(run, "preview", prefix=prefix)
        session = boto3.session.Session(region_name=site.region)
        with phase("sync"):
            result = _sync_to(run, session, prefix, path, run.settings.sync.preview_cache_control)
        url = site.preview_url(pr)
        (run.artifacts / "preview-comment.md").write_text(
            report.preview_comment(url, pr, site.domain, github.provenance(tool_version()))
        )
        github.write_output("preview_url", url)
        github.append_summary(report.sync_summary(f"Preview for #{pr}", url, result, None))
        run.logger.info("preview: %s", url)

    _command(body)(ctx)


@preview_app.command("remove")
def preview_remove(
    ctx: typer.Context,
    pr: Annotated[int, typer.Option("--pr", min=1, help="Pull request number.")],
) -> None:
    """Delete previews/pr-<n>/. Idempotent."""

    def body(run: Run) -> None:
        site = run.site
        prefix = site.preview_prefix(pr)
        _preflight(run, "preview", prefix=prefix)
        session = boto3.session.Session(region_name=site.region)
        with phase("remove"):
            count = sync.delete_prefix(session.client("s3"), site.bucket, prefix, run.logger)
        (run.artifacts / "preview-comment.md").write_text(
            report.preview_removed_comment(pr, site.domain, github.provenance(tool_version()))
        )
        github.append_summary(
            report.run_summary(f"Preview for #{pr} removed", {"objects deleted": str(count)})
        )

    _command(body)(ctx)


@report_app.command("plan-comment")
def report_plan_comment(
    ctx: typer.Context,
    exit_code: Annotated[
        int, typer.Option("--exit-code", help="Exit code of `terraform plan -detailed-exitcode`.")
    ],
    plan: Annotated[
        Path, typer.Option("--plan", help="The saved `terraform plan -no-color` output.")
    ],
    subject: Annotated[str, typer.Option("--subject", help="What was planned, for the title.")],
    out: Annotated[
        Path | None, typer.Option("--out", help="Where to write (default: the artifacts dir).")
    ] = None,
) -> None:
    """Render the plan comment from a saved plan log. This repository's own CI uses it."""

    def body(run: Run) -> None:
        comment = report.plan_comment(
            exit_code,
            plan.read_text(),
            run.settings.plan.comment_max_bytes,
            subject,
            github.provenance(tool_version()),
        )
        target = out or run.artifacts / "plan-comment.md"
        target.write_text(comment)
        run.logger.info("wrote %s (%s)", target, report.plan_result_name(exit_code))

    _command(body)(ctx)


def _apply(run: Run, terraform: stack.Terraform) -> dict[str, object]:
    with phase("init"):
        terraform.init()
    with phase("plan"):
        planfile = Path(terraform.workdir or ".") / "tfplan"
        terraform.plan(out=planfile)
    with phase("apply"):
        terraform.apply(planfile)
    outputs = terraform.outputs()
    (run.artifacts / "outputs.json").write_text(
        json.dumps(outputs, indent=2, sort_keys=True) + "\n"
    )
    return outputs


def _publish(run: Run, outputs: dict[str, object], environment: str, dist: Path) -> None:
    site = run.site
    prefix = PROD_PREFIX if environment == "prod" else DEV_PREFIX
    distribution_key = f"{environment}_distribution_id"
    distribution_id = outputs.get(distribution_key)
    if not distribution_id:
        raise PreflightError(
            f"the stack has no {environment} distribution (output {distribution_key})"
        )
    session = boto3.session.Session(region_name=site.region)
    with phase("sync"):
        result = _sync_to(run, session, prefix, dist, run.settings.sync.cache_control)
    with phase("invalidate"):
        invalidation = aws.invalidate_distribution(
            session.client("cloudfront"),
            str(distribution_id),
            reference=f"jwall-engineering-sites-{github.head_sha() or 'local'}-{environment}",
        )
        run.logger.info("invalidation %s created on %s", invalidation, distribution_id)
    url = str(
        outputs.get(f"{environment}_url")
        or (site.prod_url if environment == "prod" else site.dev_url)
    )
    github.write_output(f"{environment}_url", url)
    github.append_summary(
        report.sync_summary(f"Published {environment}", url, result, invalidation)
    )


@app.command()
def apply(ctx: typer.Context) -> None:
    """Apply the site's infrastructure and write outputs.json."""

    def body(run: Run) -> None:
        _preflight(run, "apply", prefix="")

        def tf(terraform: stack.Terraform) -> None:
            outputs = _apply(run, terraform)
            github.append_summary(
                report.run_summary(
                    "Terraform apply",
                    {"project": run.project.project, "outputs": ", ".join(sorted(outputs))},
                )
            )

        _with_terraform(run, tf)

    _command(body)(ctx)


@app.command()
def publish(
    ctx: typer.Context,
    dist: Annotated[Path | None, typer.Option("--dist", help="Built site directory.")] = None,
    environment: Annotated[str, typer.Option("--environment", help="prod or dev.")] = "prod",
) -> None:
    """Sync a built site to prod/ (or dev/) and invalidate its distribution."""

    def body(run: Run) -> None:
        if environment not in ("prod", "dev"):
            raise PreflightError("--environment must be prod or dev")
        prefix = PROD_PREFIX if environment == "prod" else DEV_PREFIX
        path = _dist_path(run, dist)
        _preflight(run, "apply", prefix=prefix)

        def tf(terraform: stack.Terraform) -> None:
            with phase("init"):
                terraform.init()
            outputs = terraform.outputs()
            _publish(run, outputs, environment, path)

        _with_terraform(run, tf)

    _command(body)(ctx)


@app.command()
def release(
    ctx: typer.Context,
    dist: Annotated[Path | None, typer.Option("--dist", help="Built site directory.")] = None,
) -> None:
    """`apply`, then `publish --environment prod`."""

    def body(run: Run) -> None:
        path = _dist_path(run, dist)
        _preflight(run, "apply", prefix=PROD_PREFIX)

        def tf(terraform: stack.Terraform) -> None:
            outputs = _apply(run, terraform)
            _publish(run, outputs, "prod", path)

        _with_terraform(run, tf)

    _command(body)(ctx)


@app.command()
def destroy(
    ctx: typer.Context,
    yes: Annotated[bool, typer.Option("--yes", help="Confirm: destroy the stack.")] = False,
) -> None:
    """Destroy a bot project's stack. The records bucket and the secret are kept."""

    def body(run: Run) -> None:
        project = run.project
        if project.kind != "bot":
            raise PreflightError("destroy is not supported for sites")
        if not yes:
            raise PreflightError(
                f"pass --yes to destroy {project.project}: its instance, VPC and image "
                "repository are deleted"
            )
        _preflight(run, "apply", prefix=None)
        session = boto3.session.Session(region_name=project.region)
        try:
            tags = image.present_tags(session.client("ecr"), project.image_repository)
        except RunError:
            tags = set()  # no repository (never applied, or already destroyed)
        running = tasks.running_tasks(session.client("ec2"), project.project)
        if running:
            ids = ", ".join(t.id for t in running)
            raise PreflightError(
                f"task instances are running ({ids}); destroying would remove their network "
                "and role under them. Wait for them, or stop them with `task terminate --all` "
                "(as the task role, or with admin credentials outside CI)"
            )
        if tags:
            raise PreflightError(
                f"{project.image_repository} still holds {len(tags)} image tag(s); the repository "
                "refuses deletion while it has images. Delete them first with admin credentials: "
                f"aws ecr batch-delete-image --repository-name {project.image_repository} "
                '--image-ids "$(aws ecr list-images --repository-name '
                f'{project.image_repository} --query imageIds --output json)"'
            )

        def tf(terraform: stack.Terraform) -> None:
            with phase("init"):
                terraform.init()
            # The records bucket has prevent_destroy, which would reject the whole
            # plan. Releasing it from state keeps the bucket and its games.
            if any(a.startswith("module.records") for a in terraform.state_list()):
                with phase("keep records"):
                    terraform.state_rm("module.records")
                    run.logger.info(
                        "released %s from Terraform state; the bucket and its games are kept",
                        project.records_bucket,
                    )
            # The task results bucket too; it expires its own contents.
            if any(a.startswith("module.task_results") for a in terraform.state_list()):
                with phase("keep task results"):
                    terraform.state_rm("module.task_results")
                    run.logger.info(
                        "released %s from Terraform state; it expires its contents on its own",
                        project.task_bucket,
                    )
            with phase("destroy"):
                terraform.destroy()

        _with_terraform(run, tf)
        run.logger.info(
            "destroyed %s; kept: the records bucket %s (no longer managed by Terraform) and "
            "the secret parameters, which Terraform never owned",
            project.project,
            project.records_bucket,
        )

    _command(body)(ctx)


@app.command()
def outputs(ctx: typer.Context) -> None:
    """Print the stack's outputs as JSON."""

    def body(run: Run) -> None:
        _preflight(run, "plan", prefix=None)

        def tf(terraform: stack.Terraform) -> None:
            with phase("init"):
                terraform.init()
            print(json.dumps(terraform.outputs(), indent=2, sort_keys=True))

        _with_terraform(run, tf)

    _command(body)(ctx)


@config_app.command("show")
def config_show(ctx: typer.Context) -> None:
    """Print the effective tool configuration as JSON."""
    run: Run = ctx.obj
    print(json.dumps(run.settings.model_dump(mode="json"), indent=2, sort_keys=True))


# -- bot projects ----------------------------------------------------------------


def _bot_project(run: Run) -> Project:
    project = run.project
    if project.kind != "bot":
        raise PreflightError(f"{project.project} is a site; the bot commands work on bot projects")
    return project


def _bot_session(run: Run, roles: list[Role] | None) -> boto3.session.Session:
    """A session in the project's region, after checking the role when `roles` is given."""
    project = _bot_project(run)
    session = boto3.session.Session(region_name=project.region)
    with phase("preflight"):
        if roles is None:
            _, arn = aws.caller_identity(session.client("sts"))
            run.logger.info("running as %s", arn)
        else:
            aws.check_any_role(
                session.client("sts"), [project.role_name(r) for r in roles], run.logger
            )
    return session


def _secret_parameter(project: Project, name: str) -> str:
    try:
        return project.bot.secrets[name]
    except KeyError:
        declared = ", ".join(sorted(project.bot.secrets))
        raise ConfigError(
            f"{name} is not a declared secret; the project declares {declared}"
        ) from None


@secrets_app.command("set")
def secrets_set(
    ctx: typer.Context,
    name: Annotated[
        str, typer.Argument(help="Secret name from lichess-bot.secrets, e.g. LICHESS_TOKEN.")
    ],
) -> None:
    """Prompt for a secret (no echo) and write it as a SecureString. Run from the owner's shell."""

    def body(run: Run) -> None:
        project = _bot_project(run)
        parameter = _secret_parameter(project, name)
        session = _bot_session(run, roles=None)
        value = typer.prompt(f"{name}", hide_input=True)
        if not value.strip():
            raise PreflightError(f"{name} must not be empty")
        botlib.put_secret(session.client("ssm"), parameter, value.strip())
        run.logger.info(
            "wrote %s to %s; a running bot picks it up with `bot deploy --restart`", name, parameter
        )

    _command(body)(ctx)


@secrets_app.command("check")
def secrets_check(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="Secret name from lichess-bot.secrets.")],
) -> None:
    """Exit 0 if the secret exists, 3 if not. Never reads its value."""

    def body(run: Run) -> None:
        project = _bot_project(run)
        parameter = _secret_parameter(project, name)
        session = _bot_session(run, roles=None)
        if not botlib.secret_exists(session.client("ssm"), parameter):
            raise PreflightError(f"{name} ({parameter}) is not set")
        run.logger.info("%s (%s) is set", name, parameter)

    _command(body)(ctx)


@bot_app.command("status")
def bot_status(ctx: typer.Context) -> None:
    """The instance, desired and running versions, rollout status and last records sync."""

    def body(run: Run) -> None:
        project = _bot_project(run)
        session = _bot_session(run, roles=["plan", "release"])
        print(botlib.render_status(botlib.status(session, project)))

    _command(body)(ctx)


@bot_app.command("logs")
def bot_logs(
    ctx: typer.Context,
    since: Annotated[str, typer.Option("--since", help="How far back, e.g. 10m, 2h.")] = "10m",
    follow: Annotated[
        bool, typer.Option("--follow", "-f", help="Keep printing new events.")
    ] = False,
) -> None:
    """Print the bot container's CloudWatch logs."""

    def body(run: Run) -> None:
        project = _bot_project(run)
        try:
            seconds = parse_duration(since)
        except ValueError as exc:
            raise ConfigError(f"--since: {exc}") from exc
        session = _bot_session(run, roles=["plan"])
        for line in botlib.tail_logs(
            session.client("logs"), project.log_group, seconds, follow, run.logger
        ):
            print(line, flush=True)

    _command(body)(ctx)


# -- images and rollouts ------------------------------------------------------------


def _version(value: str) -> str:
    if not releases.RELEASE_TAG.match(value):
        raise typer.BadParameter("must be a release tag like v1.2.3")
    return value


VersionOption = Annotated[
    str, typer.Option("--version", help="Release tag, e.g. v0.14.0.", callback=_version)
]


def _build(run: Run, project: Project, tag: str) -> str:
    """Download, verify, extract, build and smoke-test; returns the local image name."""
    work = run.artifacts / "build" / tag
    with phase("release"):
        found = releases.list_releases(project.config.repository)
        releases.find(found, tag)  # a published vX.Y.Z release, not a draft or prerelease
        asset, checksum = releases.asset_names(project.image, tag)
        release = releases.wait_for_assets(
            project.config.repository,
            tag,
            (asset, checksum),
            run.settings.bot.asset_wait_seconds,
            run.logger,
        )
        archive = releases.download(releases.asset_url(release, asset), work / "download" / asset)
        check = releases.download(
            releases.asset_url(release, checksum), work / "download" / checksum
        )
        digest = releases.verify_sha256(archive, check)
        run.logger.info("%s: sha256 %s verified", asset, digest)
        binary = releases.extract_binary(archive, project.image.artifact.binary, work / "engine")
    with phase("build"):
        # Paths in the project file (lichess/config.yml, dockerfile) are relative to
        # the file's own directory, wherever the command runs from.
        consumer_root = run.site_file.resolve().parent
        context = image.prepare_context(project, tag, binary, work / "context", consumer_root)
        name = image.build(context, project, tag, run.logger)
    with phase("smoke test"):
        nodes = image.smoke_test(name, project, run.logger)
    (run.artifacts / "build.json").write_text(
        json.dumps({"tag": tag, "image": name, "sha256": digest, "nodes": nodes}, indent=2) + "\n"
    )
    github.append_summary(
        report.run_summary(
            f"Built {name}",
            {"release asset": asset, "sha256": digest, "bench nodes (depth 6)": nodes},
        )
    )
    return name


def _push(run: Run, project: Project, session: boto3.session.Session, tag: str) -> str:
    ecr = session.client("ecr")
    if image.tag_exists(ecr, project.image_repository, tag):
        raise PreflightError(f"{project.image_repository}:{tag} already exists; tags are immutable")
    with phase("push"):
        registry = image.login(ecr, run.logger)
        return image.push(project, registry, tag, run.logger)


def _deploy(
    run: Run, project: Project, session: boto3.session.Session, tag: str, restart: bool
) -> None:
    ssm = session.client("ssm")
    if not image.tag_exists(session.client("ecr"), project.image_repository, tag):
        raise PreflightError(
            f"{project.image_repository}:{tag} is not in the repository; push it first"
        )
    instance = botlib.find_instance(session.client("ec2"), project.project)
    # Not "pending": its SSM agent is not registered yet and would refuse the
    # command; a booting instance converges to the desired version on its own.
    if instance is None or instance.state != "running":
        raise PreflightError(
            f"no running instance tagged tag:Project={project.project}, "
            f"tag:Component={botlib.INSTANCE_COMPONENT}"
            + (f" ({instance.id} is {instance.state})" if instance else "")
            + "; run `apply` first"
        )
    before = botlib.get_parameters(ssm, [project.parameter("running-version")])[
        project.parameter("running-version")
    ]
    with phase("deploy"):
        botlib.put_parameter(ssm, project.parameter("desired-version"), tag)
        # Not finished until the reconciler writes ok or failed: a stale `ok`
        # from the previous run must never end this deploy early.
        botlib.put_parameter(ssm, project.parameter("status"), f"{rollout.PENDING}{tag}")
        command_id = rollout.send_reconcile(ssm, instance.id, restart=restart)
        run.logger.info(
            "asked %s to reconcile to %s; it waits for the game in progress", instance.id, tag
        )
        outcome = rollout.follow(ssm, command_id, instance.id, project, tag, run.logger)
    if outcome == "superseded":
        run.logger.info("%s was superseded by a newer deploy; that one is being shipped", tag)
        github.append_summary(
            report.run_summary(
                f"{tag} superseded", {"instance": instance.id, "previous": before or "nothing"}
            )
        )
        return
    run.logger.info("deployed %s (was %s)", tag, before or "nothing")
    github.write_output("version", tag)
    github.append_summary(
        report.run_summary(
            f"Deployed {tag}",
            {"instance": instance.id, "previous": before or "nothing", "now": tag},
        )
    )


@bot_app.command("build")
def bot_build(
    ctx: typer.Context,
    version: VersionOption,
    from_source: Annotated[
        bool, typer.Option("--from-source", help="Reserved: rebuild the engine from source.")
    ] = False,
) -> None:
    """Build the image for a release from its verified Linux asset; smoke-test it. Docker only."""

    def body(run: Run) -> None:
        project = _bot_project(run)
        if from_source:
            raise PreflightError(
                "--from-source is not implemented in v1; build from the release asset"
            )
        _build(run, project, version)

    _command(body)(ctx)


@bot_app.command("push")
def bot_push(ctx: typer.Context, version: VersionOption) -> None:
    """Push a built image to ECR under its release tag. Refuses an existing tag."""

    def body(run: Run) -> None:
        project = _bot_project(run)
        session = _bot_session(run, roles=["release"])
        _push(run, project, session, version)

    _command(body)(ctx)


@bot_app.command("deploy")
def bot_deploy(
    ctx: typer.Context,
    version: VersionOption,
    restart: Annotated[
        bool,
        typer.Option("--restart", help="Swap even if this version runs (e.g. a rotated secret)."),
    ] = False,
) -> None:
    """Roll the bot to a pushed version and follow the swap to its end."""

    def body(run: Run) -> None:
        project = _bot_project(run)
        session = _bot_session(run, roles=["release"])
        _deploy(run, project, session, version, restart)

    _command(body)(ctx)


@bot_app.command("reconcile")
def bot_reconcile(
    ctx: typer.Context,
    version: Annotated[
        str | None,
        typer.Option("--version", help="Release tag; default: the newest release."),
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Print the plan as JSON and change nothing.")
    ] = False,
) -> None:
    """Build and push the release if it is missing, then deploy it if it is the newest.
    Safe to repeat. The release workflow runs this."""

    def body(run: Run) -> None:
        project = _bot_project(run)
        if version is not None:
            _version(version)
        session = _bot_session(run, roles=None if dry_run else ["release"])
        found = releases.list_releases(project.config.repository)
        present = image.present_tags(session.client("ecr"), project.image_repository)
        running = botlib.get_parameters(
            session.client("ssm"), [project.parameter("running-version")]
        )[project.parameter("running-version")]
        plan = rollout.decide(version, found, present, running, project.bot.track)
        run.logger.info("plan: %s", plan.reason)
        if dry_run:
            print(json.dumps(plan.as_dict(), sort_keys=True))
            return
        if plan.build:
            _build(run, project, plan.version)
        if plan.push:
            _push(run, project, session, plan.version)
        if plan.deploy:
            _deploy(run, project, session, plan.version, restart=False)

    _command(body)(ctx)


# -- remote tasks -----------------------------------------------------------------


#: The lifetime guard counts from the task's start, after setup: sweep only
#: instances older than their lifetime plus this headroom.
SWEEP_SETUP_HOURS = 1


def _now() -> datetime:
    return datetime.now(UTC)


def _task_runner(project: Project) -> TaskRunner:
    runner = project.task_runner
    if runner is None:
        raise ConfigError(f"{project.project} has no task-runner component")
    return runner


def _default_run_id() -> str:
    run_id = os.environ.get("GITHUB_RUN_ID")
    if run_id:
        return f"{run_id}-{os.environ.get('GITHUB_RUN_ATTEMPT', '1')}"
    return _now().strftime("local-%Y%m%dT%H%M%SZ")


@task_app.command("run", context_settings={"allow_interspersed_args": False})
def task_run(
    ctx: typer.Context,
    script: Annotated[
        str, typer.Option("--script", help="Task script, relative to the repository root.")
    ],
    ref: Annotated[str, typer.Option("--ref", help="Commit SHA of the repository to run.")],
    args: Annotated[
        list[str] | None, typer.Argument(help="Arguments for the script, after --.")
    ] = None,
    profile_name: Annotated[
        str,
        typer.Option("--profile", help="The task profile: cpu, or one of task-runner.profiles."),
    ] = CPU_PROFILE,
    instance_type: Annotated[
        str | None, typer.Option("--instance-type", help="One of the profile's instance_types.")
    ] = None,
    max_hours: Annotated[
        int | None, typer.Option("--max-hours", help="Hard limit; at most the profile's max_hours.")
    ] = None,
    out: Annotated[Path, typer.Option("--out", help="Where the task's outputs are copied.")] = Path(
        "out"
    ),
    run_id: Annotated[
        str | None, typer.Option("--run-id", help="Default: GITHUB_RUN_ID-GITHUB_RUN_ATTEMPT.")
    ] = None,
    task_id: Annotated[str | None, typer.Option("--task-id", help="Default: GITHUB_JOB.")] = None,
    market: Annotated[
        str | None,
        typer.Option("--market", help="spot or on-demand; default the profile's market."),
    ] = None,
    max_price_fraction: Annotated[
        float | None,
        typer.Option(
            "--max-price-fraction",
            help="Spot price cap, a fraction of on demand; default from the project file.",
        ),
    ] = None,
    on_demand_fallback: Annotated[
        bool | None,
        typer.Option(
            "--on-demand-fallback/--no-on-demand-fallback",
            help="On demand at launch when spot has no capacity; default from the project file.",
        ),
    ] = None,
    pull_request: Annotated[
        str | None, typer.Option("--pull-request", help="Pull request number, for the tags.")
    ] = None,
) -> None:
    """Run one script on a fresh instance (spot by default) and exit with its code.

    The instance is terminated whatever happens. Exit codes: the task's own,
    1 for a runtime failure, 2 configuration, 3 preflight or no capacity, 75 spot
    interruption (never a result: re-run), 124 timed out, 130 cancelled.
    """

    def body(run: Run) -> None:
        # Until the task reports otherwise, any refusal reads as failed.
        github.write_output("outcome", "failed")
        github.write_output("interrupted-at", "")
        project = _bot_project(run)
        runner = _task_runner(project)
        if profile_name not in runner.profile_names:
            known = ", ".join(runner.profile_names)
            raise ConfigError(f"--profile {profile_name} is not a task profile; use one of {known}")
        profile = runner.profile(profile_name)
        chosen = instance_type or profile.default_instance_type
        if chosen not in profile.instance_types:
            allowed = ", ".join(profile.instance_types)
            raise ConfigError(
                f"--instance-type {chosen} is not allowed in the {profile_name} profile; "
                f"use one of {allowed}"
            )
        hours = max_hours if max_hours is not None else profile.max_hours
        if not 1 <= hours <= profile.max_hours:
            raise ConfigError(
                f"--max-hours must be 1 to the {profile_name} profile's max_hours "
                f"({profile.max_hours})"
            )
        chosen_market = market or profile.market
        if chosen_market not in ("spot", "on-demand"):
            raise ConfigError(f"--market must be spot or on-demand, got {chosen_market}")
        fraction = profile.max_price_fraction if max_price_fraction is None else max_price_fraction
        if not 0 < fraction <= 1:
            raise ConfigError(f"--max-price-fraction must be above 0 and at most 1, got {fraction}")
        if pull_request is not None and not pull_request.isdigit():
            raise ConfigError(f"--pull-request must be a number, got {pull_request}")
        spec = tasks.TaskSpec(
            project=project,
            instance_type=chosen,
            max_hours=hours,
            run_id=run_id or _default_run_id(),
            task_id=task_id or os.environ.get("GITHUB_JOB") or "local",
            workflow=os.environ.get("GITHUB_WORKFLOW") or "local",
            repository=project.config.repository,
            ref=ref,
            script=script,
            args=tuple(args or ()),
            market=chosen_market,
            max_price_fraction=fraction,
            on_demand_fallback=(
                profile.on_demand_fallback if on_demand_fallback is None else on_demand_fallback
            ),
            spot_wait_minutes=profile.spot_wait_minutes,
            pull_request=pull_request,
            profile=profile_name,
            whole_cores=profile.whole_cores,
            gpu=profile.gpu,
            scratch=profile.scratch,
        )
        session = _bot_session(run, roles=["task"])
        run.logger.info(
            "task %s/%s: %s on %s %s, %s profile (at most %dh)",
            spec.run_id, spec.task_id, script, chosen, chosen_market, profile_name, hours,
        )  # fmt: skip
        code = tasks.run_task(
            session,
            spec,
            out,
            run.logger,
            write=lambda line: print(line, flush=True),
            poll=run.settings.tasks.poll_seconds,
        )
        if code:
            raise typer.Exit(code)

    _command(body)(ctx)


def _render_tasks(instances: list[tasks.TaskInstance]) -> str:
    if not instances:
        return "no task instances running"
    lines = [
        f"{'instance':<21} {'profile':<8} {'type':<13} {'state':<9} {'run':<14} {'task':<14} "
        "launched"
    ]
    for t in instances:
        lines.append(
            f"{t.id:<21} {t.profile:<8} {t.type:<13} {t.state:<9} {t.run_id or '-':<14} "
            f"{t.task_id or '-':<14} {t.launched_at:%Y-%m-%d %H:%M}Z"
        )
    return "\n".join(lines)


@task_app.command("list")
def task_list(ctx: typer.Context) -> None:
    """List the project's running task instances. Read-only."""

    def body(run: Run) -> None:
        project = _bot_project(run)
        session = _bot_session(run, roles=["plan", "task"])
        print(_render_tasks(tasks.running_tasks(session.client("ec2"), project.project)))

    _command(body)(ctx)


@task_app.command("terminate")
def task_terminate(
    ctx: typer.Context,
    instance: Annotated[
        str | None, typer.Option("--instance", help="One task instance ID.")
    ] = None,
    run_id: Annotated[str | None, typer.Option("--run-id", help="Every task of this run.")] = None,
    task_id: Annotated[
        str | None, typer.Option("--task-id", help="With --run-id: only this task.")
    ] = None,
    all_: Annotated[bool, typer.Option("--all", help="Every running task instance.")] = False,
) -> None:
    """Terminate task instances. Only instances tagged Purpose=ci-task of this project match."""

    def body(run: Run) -> None:
        project = _bot_project(run)
        if sum([instance is not None, run_id is not None, all_]) != 1 or (
            task_id is not None and run_id is None
        ):
            raise ConfigError("give exactly one of --instance, --run-id [--task-id] or --all")
        session = _bot_session(run, roles=["task"])
        ec2 = session.client("ec2")
        running = tasks.running_tasks(ec2, project.project)
        if instance is not None:
            chosen = [t for t in running if t.id == instance]
            if not chosen:
                raise PreflightError(
                    f"{instance} is not a running task instance of {project.project}"
                )
        elif run_id is not None:
            chosen = [
                t
                for t in running
                if t.run_id == run_id and (task_id is None or t.task_id == task_id)
            ]
        else:
            chosen = running
        if not chosen:
            run.logger.info("no task instance matches; nothing to terminate")
        for t in chosen:
            tasks.terminate(ec2, t.id, run.logger)

    _command(body)(ctx)


@task_app.command("sweep")
def task_sweep(
    ctx: typer.Context,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="List, terminate nothing.")] = False,
) -> None:
    """Terminate task instances past their MaxLifetimeHours. Run by hand: leaks are
    normally impossible (the CLI, the workflow's cleanup step and the instance's
    own shutdown all terminate), so there is no scheduled sweeper."""

    def body(run: Run) -> None:
        project = _bot_project(run)
        runner = _task_runner(project)
        session = _bot_session(run, roles=["task"])
        ec2 = session.client("ec2")
        now = _now()
        expired = [
            t
            for t in tasks.running_tasks(ec2, project.project)
            if now - t.launched_at
            > timedelta(hours=(t.max_hours or runner.max_hours) + SWEEP_SETUP_HOURS)
        ]
        if not expired:
            run.logger.info("no task instance is past its lifetime")
        for t in expired:
            age = (now - t.launched_at).total_seconds() / 3600
            if dry_run:
                run.logger.info("would terminate %s (%.1fh old, run %s)", t.id, age, t.run_id)
            else:
                run.logger.warning("terminating %s (%.1fh old, run %s)", t.id, age, t.run_id)
                tasks.terminate(ec2, t.id, run.logger)

    _command(body)(ctx)


if __name__ == "__main__":  # pragma: no cover
    app()
