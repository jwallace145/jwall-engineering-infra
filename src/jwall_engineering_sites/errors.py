"""Failure classes, each with the exit code the CLI maps it to.

Distinct codes make the failing layer obvious from a job summary:
1 is Terraform or AWS, 2 is configuration, 3 is a preflight check.
"""


class SitesError(Exception):
    """Base class: a failure the CLI reports and exits on."""

    exit_code = 1


class ConfigError(SitesError):
    """config.yaml or the site file is invalid."""

    exit_code = 2


class PreflightError(SitesError):
    """A check that runs before Terraform or S3 is touched failed."""

    exit_code = 3


class RunError(SitesError):
    """Terraform or an AWS call failed."""

    exit_code = 1
