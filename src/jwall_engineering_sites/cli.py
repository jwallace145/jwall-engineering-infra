"""The `jwall-engineering-sites` command line.

Every command loads config.yaml (baked into the image) and the site file
(from the working directory), derives the rest, runs its preflight checks and
then Terraform or S3. Failures map to exit codes: 1 Terraform/AWS, 2
configuration, 3 preflight.
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Callable
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
from pathlib import Path
from typing import Annotated, Literal

import boto3
import typer

from . import aws, github, report, stack, sync
from .errors import PreflightError, SitesError
from .log import annotate_error, artifacts_dir, phase, setup_logging
from .settings import Settings, default_config_path, load_settings
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
app.add_typer(preview_app, name="preview")
app.add_typer(config_app, name="config")

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
    return stack.Terraform(run.site, run.settings, run.logger)


def _preflight(run: Run, role: Literal["plan", "preview", "apply"], prefix: str | None) -> None:
    with phase("preflight"):
        site = run.site
        session = boto3.session.Session(region_name=site.region)
        aws.check_role(session.client("sts"), site.role_name(role), run.logger)
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
    path = dist if dist is not None else Path(run.site.config.site.output_dir)
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
        immutable_paths=site.config.site.immutable_paths,
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
    """Validate config.yaml and the site file, then `terraform validate` offline."""

    def body(run: Run) -> None:
        site = run.site
        with phase("validate"):
            run.logger.info("site file ok: project %s, domain %s", site.project, site.config.domain)
            run.logger.info("derived: bucket %s, state key %s", site.bucket, site.state_key)

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
                github.head_sha(),
                github.run_url(),
                tool_version(),
            )
            (run.artifacts / "plan-comment.md").write_text(comment)
            github.write_output("result", report.plan_result_name(exit_code))
            github.write_output("changes", "true" if exit_code == stack.PLAN_CHANGES else "false")
        github.append_summary(
            report.run_summary(
                f"Terraform plan: {report.plan_result_name(exit_code)}",
                {"site": run.site.project, "tool": tool_version()},
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
            report.preview_comment(url, github.head_sha())
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
        (run.artifacts / "preview-comment.md").write_text(report.preview_removed_comment())
        github.append_summary(
            report.run_summary(f"Preview for #{pr} removed", {"objects deleted": str(count)})
        )

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
                    {"site": run.site.project, "outputs": ", ".join(sorted(outputs))},
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


if __name__ == "__main__":  # pragma: no cover
    app()
