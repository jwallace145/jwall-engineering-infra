"""One `validate`, `plan`, `apply`, `destroy` or `outputs` across a Databricks
project's two Terraform stacks.

The account stack (network, workspace, metastore assignment, members) comes
first; the workspace stack (catalog, compute policies, settings) takes its
outputs. Checks run before (`config`, `live`), between (`readiness`) and after
(`verify`) the stacks. Every step appends a started/success|skipped|failure
event. A Terraform failure is diagnosed, written to the job summary and raised
as a `RunError`; a re-run converges because each step only does what is left.

The Databricks client secret reaches Terraform through its environment only.
While a command runs, every log record (console and debug log) and every
Terraform text that lands in a file or comment is masked with it.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, NoReturn

from . import checks, databricks_destroy, github, report, stack
from .checks import render as checks_render
from .checks.framework import (
    CheckContext,
    CheckResult,
    Outcome,
    Severity,
    Stage,
    raise_on_errors,
    run_stage,
)
from .databricks_names import derived_names
from .diagnose import Failure, _redact, diagnose, render_failure
from .errors import RunError
from .events import EventLog, Result
from .log import ARTIFACTS_DIR, LOG_FILE_NAME, phase
from .project import Project

if TYPE_CHECKING:
    import boto3.session

    from .cli import Run
    from .databricks_auth import DatabricksClients

#: The account stack's workspace: replacing it makes the workspace stack's plan stale.
WORKSPACE_RESOURCE = "module.workspace.databricks_mws_workspaces.this"
DEFERRED = "workspace stack is planned after the workspace exists"
#: The workflow that applies a Databricks project (merging a PR does not).
APPLIED_BY = "Databricks apply"
SETUP_RUNBOOK = "docs/databricks.md#one-time-setup"
_RERUN_APPLY = "re-running apply is safe"
_RERUN_DESTROY = "Re-running destroy is safe: every step continues where this one stopped."

#: The event stage of each check stage.
CHECK_STEPS: Mapping[Stage, str] = {
    Stage.CONFIG: "checks-config",
    Stage.LIVE: "checks-live",
    Stage.BETWEEN: "readiness",
    Stage.AFTER: "verify",
}

TerraformFactory = Callable[..., stack.Terraform]


@dataclass
class StackRun:
    stack: str
    result: Literal["applied", "planned", "deferred", "failed", "skipped"]
    failure: Failure | None
    #: `terraform plan -detailed-exitcode`'s code; None when the stack was not planned.
    exit_code: int | None = None
    #: The plan's text (or the failure's error), secret masked.
    plan_text: str = ""
    #: The account plan replaces the workspace, so this plan will not be the one applied.
    stale: bool = False


class StackFailedError(RunError):
    """Terraform failed in one stack; `failure` is its diagnosis."""

    def __init__(self, failure: Failure) -> None:
        cause = f" ({failure.diagnosis.error_class})" if failure.diagnosis else ""
        super().__init__(f"terraform failed in {failure.stack}{cause}")
        self.failure = failure


def workspace_plan_stale(account_plan: str) -> bool:
    """Whether the account plan deletes or replaces the workspace."""
    return any(
        change.address == WORKSPACE_RESOURCE and change.action in ("replace", "destroy")
        for change in report.parse_plan(account_plan).resources
    )


class _Redact(logging.Filter):
    """Masks the secret in every record, its traceback included."""

    def __init__(self, secrets: Sequence[str]) -> None:
        super().__init__()
        self._secrets = secrets

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if record.exc_info:
            message += "\n" + logging.Formatter().formatException(record.exc_info)
            record.exc_info = None
            record.exc_text = None
        record.msg = _redact(message, self._secrets)
        record.args = None
        return True


@contextmanager
def redacting(logger: logging.Logger, secrets: Sequence[str]) -> Iterator[None]:
    """Mask `secrets` in everything `logger` writes until the block ends."""
    guard = _Redact(secrets)
    logger.addFilter(guard)
    try:
        yield
    finally:
        logger.removeFilter(guard)


class _Step:
    def __init__(self) -> None:
        self.skipped = False
        self.failed_checks: list[str] = []

    def skip(self) -> None:
        self.skipped = True


class DatabricksDeployment:
    def __init__(
        self,
        run: Run,
        project: Project,
        events: EventLog,
        clients: DatabricksClients | None,
        terraform_factory: TerraformFactory,
        *,
        session: boto3.session.Session | None = None,
        secret: str = "",
        version: str = "0.0.0+local",
    ) -> None:
        """`terraform_factory(stack=..., tfvars_override=...)` builds one stack's
        Terraform (credentials already in its environment). `session` is the CI
        role's AWS session (None offline); `secret` is only used to mask output."""
        self.run = run
        self.project = project
        self.settings = run.settings
        self.logger = run.logger
        self.events = events
        self.clients = clients
        self.session = session
        self._factory = terraform_factory
        self._secrets = (secret,) if secret else ()
        self._version = version
        self._event = "validate"
        self._workspace_id: str | None = None
        self.results: list[CheckResult] = []

    @property
    def debug_log(self) -> Path:
        """Relative, as the workflow run's artifact holds it."""
        return ARTIFACTS_DIR / LOG_FILE_NAME

    # -- commands -------------------------------------------------------------

    def validate(self) -> None:
        """CONFIG checks, then `terraform validate` of both stacks, offline."""
        self._event = "validate"
        with redacting(self.logger, self._secrets):
            self._checks([Stage.CONFIG])
            try:
                with self._step("validate"):
                    # The workspace stack's inputs need the account's outputs; validate
                    # reads no variable values, so it gets none.
                    for name, override in (("account", None), ("workspace", {})):
                        with (
                            phase(f"validate {name} stack"),
                            self._terraform(name, override) as tf,
                            self._diagnosed(tf),
                        ):
                            tf.prepare()
                            tf.init(backend=False)
                            tf.validate()
            except StackFailedError as exc:
                self._fail(exc.failure)
            self.logger.info("both stacks validate")

    def plan(self) -> list[StackRun]:
        """LIVE checks, the account plan, then the workspace plan, deferred or stale
        as the account state and plan say. Writes one comment for both."""
        self._event = "plan"
        runs: list[StackRun] = []
        with redacting(self.logger, self._secrets):
            try:
                self._checks([Stage.CONFIG, Stage.LIVE])
                account, outputs = self._plan_account()
                runs.append(account)
                runs.append(self._plan_workspace(account, outputs))
            finally:
                self._write_plan(runs)
        failed = [r.stack for r in runs if r.result == "failed"]
        if failed:
            raise RunError(f"the {' and '.join(failed)} stack plan failed; see the plan comment")
        return runs

    def apply(self) -> dict[str, Any]:
        """LIVE checks, the account stack, readiness, the workspace stack, verify."""
        self._event = "apply"
        with redacting(self.logger, self._secrets):
            self._checks([Stage.CONFIG, Stage.LIVE])
            try:
                account = self._apply_stack("account", None)
            except StackFailedError as exc:
                self._fail(exc.failure)
            if not account.get("workspace_url"):
                raise RunError(
                    "the account stack applied but has no workspace_url output; "
                    "check its outputs.tf and re-run apply"
                )
            self._use_account_outputs(account)
            self._checks(
                [Stage.BETWEEN],
                note=f"The account stack is applied; {_RERUN_APPLY} once the check above passes.",
            )
            tfvars = stack._databricks_workspace_tfvars(self.project, self.settings, account)
            try:
                workspace = self._apply_stack("workspace", tfvars)
            except StackFailedError as exc:
                self._fail(exc.failure, partial="account")
            self._checks([Stage.AFTER])
            merged = {**account, **workspace}
            (self.run.artifacts / "outputs.json").write_text(
                json.dumps(merged, indent=2, sort_keys=True, default=str) + "\n"
            )
            github.append_summary(
                report.run_summary(
                    "Databricks apply",
                    {
                        "project": self.project.project,
                        "workspace": str(merged.get("workspace_url")),
                        "catalog": str(merged.get("catalog_name")),
                    },
                )
            )
            return merged

    def destroy(self) -> list[str]:
        """Empty the catalog bucket, destroy the workspace stack, then the account
        stack (R7). Returns what each step removed."""
        self._event = "destroy"
        deleted: list[str] = []
        with redacting(self.logger, self._secrets):
            try:
                deleted += self._empty_catalog_bucket()
                deleted += self._destroy_workspace()
                with (
                    self._terraform("account", None) as tf,
                    self._step("account-destroy", self._extra(tf)),
                    self._diagnosed(tf),
                    phase("destroy account stack"),
                ):
                    tf.prepare()
                    tf.init()
                    tf.destroy()
                deleted.append(
                    "the account stack: workspace, VPC, NAT instance, root bucket, roles"
                )
            except StackFailedError as exc:
                self._fail(exc.failure, suffix=_RERUN_DESTROY)
        return deleted

    def outputs(self) -> dict[str, Any]:
        """Both stacks' outputs, merged; the account's alone until the workspace exists."""
        self._event = "outputs"
        with redacting(self.logger, self._secrets):
            account = self._read_outputs("account")
            if not account.get("workspace_url"):
                self.logger.info("the workspace does not exist yet: account outputs only")
                return account
            return {**account, **self._read_outputs("workspace")}

    # -- steps ----------------------------------------------------------------

    def _extra(self, tf: stack.Terraform | None = None) -> dict[str, str]:
        extra: dict[str, str] = {}
        if tf is not None:
            extra["state_key"] = tf.state_key
        if self._workspace_id is not None:
            extra["workspace_id"] = self._workspace_id
        return extra

    def _emit(self, stage: str, result: Result, **fields: Any) -> None:
        self.events.emit(self._event, stage, result, **fields)

    @contextmanager
    def _step(self, stage: str, extra: Mapping[str, str] | None = None) -> Iterator[_Step]:
        """A started event, then success, skipped or failure (with the error class)."""
        step = _Step()
        self._emit(stage, "started", extra=extra)
        started = time.monotonic()
        try:
            yield step
        except BaseException as exc:
            error_class = None
            if isinstance(exc, StackFailedError) and exc.failure.diagnosis is not None:
                error_class = exc.failure.diagnosis.error_class
            self._emit(
                stage,
                "failure",
                duration_s=round(time.monotonic() - started, 3),
                error_class=error_class,
                failed_checks=step.failed_checks,
                extra=extra,
            )
            raise
        self._emit(
            stage,
            "skipped" if step.skipped else "success",
            duration_s=round(time.monotonic() - started, 3),
            failed_checks=step.failed_checks,
            extra=extra,
        )

    def _checks(self, stages: Sequence[Stage], note: str | None = None) -> None:
        """Run check stages in order; the first with a failed ERROR check stops (exit 3)."""
        checks.load_builtin_checks()
        for stage in stages:
            with (
                self._step(CHECK_STEPS[stage], self._extra()) as step,
                phase(f"{stage.value} checks"),
            ):
                offline = stage is Stage.CONFIG
                context = CheckContext(
                    project=self.project,
                    settings=self.settings,
                    logger=self.logger,
                    session=None if offline else self.session,
                    databricks=None if offline else self.clients,
                )
                results = run_stage(stage, context)
                self.results += results
                checks_render.write_json(self.results, self.run.artifacts / "checks.json")
                if results:
                    self.logger.info("%s", checks_render.console_table(results))
                step.failed_checks = [
                    r.check_id
                    for r in results
                    if r.outcome is Outcome.FAIL and r.severity is Severity.ERROR
                ]
                if step.failed_checks:
                    summary = [
                        f"## Databricks {stage.value} checks failed",
                        "",
                        checks_render.markdown_table(results),
                    ]
                    if note:
                        summary += ["", note]
                    github.append_summary("\n".join(summary))
                    if note:
                        self.logger.info("%s", note)
                raise_on_errors(results)

    @contextmanager
    def _terraform(
        self, name: str, tfvars_override: Mapping[str, Any] | None
    ) -> Iterator[stack.Terraform]:
        """One stack's Terraform, cleaned up afterwards; the caller prepares it inside
        its step, so a failed prepare is that step's failure."""
        terraform = self._factory(stack=name, tfvars_override=tfvars_override)
        failed = True
        try:
            yield terraform
            failed = False
        finally:
            terraform.cleanup(failed=failed)

    @contextmanager
    def _diagnosed(self, terraform: stack.Terraform) -> Iterator[None]:
        """Turn a failed Terraform command into a `StackFailedError` with its diagnosis."""
        try:
            yield
        except RunError as exc:
            if exc.output is None or isinstance(exc, StackFailedError):
                raise
            output = _redact(exc.output, self._secrets)
            raise StackFailedError(diagnose(terraform.stack_name, output, self.debug_log)) from None

    def _fail(
        self, failure: Failure, partial: str | None = None, suffix: str | None = None
    ) -> NoReturn:
        """Explain the failure on the console, in the job summary and failure.md."""
        text = render_failure(failure, partial=partial)
        if suffix:
            text += f"\n{suffix}\n"
        self.logger.error("%s", text)
        github.append_summary(text)
        (self.run.artifacts / "failure.md").write_text(text)
        cause = f" ({failure.diagnosis.error_class})" if failure.diagnosis else ""
        message = f"terraform failed in {failure.stack}{cause}"
        if partial:
            message = f"{partial} stack applied; workspace stack failed; {_RERUN_APPLY}: {message}"
        raise RunError(message)

    def _use_account_outputs(self, outputs: Mapping[str, Any]) -> None:
        """The between/after checks and the events read the account stack's outputs."""
        if self.clients is not None:
            self.clients.account_outputs = dict(outputs)
        if outputs.get("workspace_id") is not None:
            self._workspace_id = str(outputs["workspace_id"])

    def _read_outputs(self, name: str) -> dict[str, Any]:
        """A stack's outputs: init and output only, so no variable is needed."""
        with self._terraform(name, {}) as tf, self._diagnosed(tf), phase(f"{name} outputs"):
            tf.prepare()
            tf.init()
            return tf.outputs()

    # -- plan -----------------------------------------------------------------

    def _plan_stack(
        self, name: str, tfvars: Mapping[str, Any] | None, read_outputs: bool = False
    ) -> tuple[StackRun, dict[str, Any]]:
        try:
            with (
                self._terraform(name, tfvars) as tf,
                self._step(f"{name}-plan", self._extra(tf)),
                self._diagnosed(tf),
                phase(f"plan {name} stack"),
            ):
                tf.prepare()
                tf.init()
                result = tf.plan(detailed_exitcode=True)
                outputs = tf.outputs() if read_outputs else {}
        except StackFailedError as exc:
            text = render_failure(exc.failure)
            self.logger.error("%s", text)
            github.append_summary(text)
            failed = StackRun(name, "failed", exc.failure, stack.PLAN_ERROR, exc.failure.raw)
            return failed, {}
        planned = StackRun(
            name, "planned", None, result.returncode, _redact(result.output, self._secrets)
        )
        return planned, outputs

    def _plan_account(self) -> tuple[StackRun, dict[str, Any]]:
        return self._plan_stack("account", None, read_outputs=True)

    def _plan_workspace(self, account: StackRun, outputs: Mapping[str, Any]) -> StackRun:
        if account.result == "failed" or not outputs.get("workspace_url"):
            with self._step("workspace-plan") as step:
                step.skip()
            if account.result == "failed":
                return StackRun("workspace", "skipped", None)
            self.logger.info("the account stack has no workspace yet: the %s", DEFERRED)
            return StackRun("workspace", "deferred", None)
        self._use_account_outputs(outputs)
        tfvars = stack._databricks_workspace_tfvars(self.project, self.settings, outputs)
        run, _ = self._plan_stack("workspace", tfvars)
        if workspace_plan_stale(account.plan_text):
            self.logger.warning(
                "the account plan replaces the workspace, so the workspace plan is stale: "
                "apply plans the workspace stack again against the new workspace"
            )
            run.stale = True
        return run

    def _write_plan(self, runs: list[StackRun]) -> None:
        sections = [self._section(r) for r in runs] or [
            report.StackSection(
                "Account stack", None, note="Not planned: a preflight check failed (see below)."
            )
        ]
        (self.run.artifacts / "plan.txt").write_text(
            "".join(f"# {r.stack} stack\n{r.plan_text}\n" for r in runs)
        )
        comment = report.stacks_plan_comment(
            sections,
            checks_render.markdown_table(self.results) if self.results else None,
            self.settings.plan.comment_max_bytes,
            self.project.project,
            github.provenance(self._version),
            applied_by=APPLIED_BY,
        )
        (self.run.artifacts / "plan-comment.md").write_text(comment)
        codes = [s.exit_code for s in sections if s.exit_code is not None]
        if not codes or any(c not in (stack.PLAN_NO_CHANGES, stack.PLAN_CHANGES) for c in codes):
            exit_code = stack.PLAN_ERROR
        else:
            exit_code = stack.PLAN_CHANGES if stack.PLAN_CHANGES in codes else 0
        github.write_output("result", report.plan_result_name(exit_code))
        github.write_output("changes", "true" if exit_code == stack.PLAN_CHANGES else "false")
        github.append_summary(
            report.run_summary(
                f"Terraform plan: {report.plan_result_name(exit_code)}",
                {"project": self.project.project, "tool": self._version},
            )
        )

    @staticmethod
    def _section(run: StackRun) -> report.StackSection:
        title = f"{run.stack.capitalize()} stack"
        note: str | None = None
        if run.failure is not None:
            note = render_failure(run.failure)
        elif run.result == "deferred":
            note = (
                f"> [!NOTE]\n> **Not planned yet:** the {DEFERRED}. The first apply creates "
                "the workspace, then plans and applies this stack."
            )
        elif run.result == "skipped":
            note = "Not planned: the account stack's plan failed."
        elif run.stale:
            note = (
                "> [!WARNING]\n> **This plan is stale.** The account plan replaces the "
                "workspace, so apply plans the workspace stack again against the new workspace."
            )
        # A failure's note already holds its error; the plan body would repeat it.
        text = "" if run.failure is not None else run.plan_text
        return report.StackSection(title, run.exit_code, text, note)

    # -- apply ----------------------------------------------------------------

    def _apply_stack(self, name: str, tfvars: Mapping[str, Any] | None) -> dict[str, Any]:
        """Plan, apply only when there are changes, and read the outputs."""
        with self._terraform(name, tfvars) as tf:
            with (
                self._step(f"{name}-plan", self._extra(tf)),
                self._diagnosed(tf),
                phase(f"plan {name} stack"),
            ):
                tf.prepare()
                tf.init()
                planfile = Path(tf.workdir or ".") / "tfplan"
                result = tf.plan(out=planfile, detailed_exitcode=True)
            with (
                self._step(f"{name}-apply", self._extra(tf)) as step,
                self._diagnosed(tf),
                phase(f"apply {name} stack"),
            ):
                if result.returncode == stack.PLAN_NO_CHANGES:
                    self.logger.info("the %s stack has no changes", name)
                    step.skip()
                else:
                    tf.apply(planfile)
                return tf.outputs()

    # -- destroy --------------------------------------------------------------

    def _empty_catalog_bucket(self) -> list[str]:
        if self.session is None:
            raise RunError("destroy needs AWS credentials to empty the catalog bucket")
        bucket = derived_names(self.project).catalog_bucket
        with self._step("empty-catalog-bucket") as step, phase("empty catalog bucket"):
            count = databricks_destroy.empty_bucket(self.session.client("s3"), bucket, self.logger)
            if count is None:
                step.skip()
                return []
        return [f"{count} object versions from s3://{bucket}"]

    def _destroy_workspace(self) -> list[str]:
        with self._step("workspace-destroy") as step:
            account = self._read_outputs("account")
            if account.get("workspace_url"):
                self._use_account_outputs(account)
                tfvars = stack._databricks_workspace_tfvars(self.project, self.settings, account)
                with (
                    self._terraform("workspace", tfvars) as tf,
                    self._diagnosed(tf),
                    phase("destroy workspace stack"),
                ):
                    tf.prepare()
                    tf.init()
                    tf.destroy()
                return ["the workspace stack: catalog, catalog bucket, compute policies"]
            # No workspace: a previous run destroyed this stack, then part of the account's.
            with self._terraform("workspace", {}) as tf, self._diagnosed(tf):
                tf.prepare()
                tf.init()
                left = tf.state_list()
            if left:
                raise RunError(
                    f"the workspace stack still holds {len(left)} resource(s) but the account "
                    "stack has no workspace_url output, so they cannot be reached; re-apply the "
                    "account stack, or remove them by hand (see docs/databricks.md#account-state)"
                )
            self.logger.info("the workspace stack is already destroyed")
            step.skip()
            return []
