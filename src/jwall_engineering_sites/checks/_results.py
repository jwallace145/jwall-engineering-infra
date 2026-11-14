"""A small builder so each check states its id, title and runbook once."""

from __future__ import annotations

from dataclasses import dataclass

from ..diagnose import RUNBOOK_DOC
from .framework import CheckResult, Outcome, Severity


@dataclass(frozen=True)
class Meta:
    check_id: str
    title: str
    anchor: str
    severity: Severity = Severity.ERROR

    @property
    def runbook(self) -> str:
        return f"{RUNBOOK_DOC}#{self.anchor}"

    def _result(
        self,
        outcome: Outcome,
        facts: dict[str, str] | None,
        remediation: str | None,
        runbook: str | None = None,
    ) -> CheckResult:
        return CheckResult(
            check_id=self.check_id,
            title=self.title,
            outcome=outcome,
            severity=self.severity,
            facts=facts or {},
            remediation=remediation,
            runbook=runbook or self.runbook,
        )

    def passed(self, **facts: str) -> CheckResult:
        return self._result(Outcome.PASS, facts, None)

    def failed(self, remediation: str, **facts: str) -> CheckResult:
        return self._result(Outcome.FAIL, facts, remediation)

    def skipped(self, remediation: str, runbook: str | None = None, **facts: str) -> CheckResult:
        return self._result(Outcome.SKIP, facts, remediation, runbook)
