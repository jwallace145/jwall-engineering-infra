# jwall-engineering-sites: the CLI, a pinned Terraform, the site and bot stacks
# with their modules and providers vendored, and config.yaml. Only the S3 backend touches
# the network at run time.
#
# Build: docker build --build-arg TERRAFORM_VERSION=<terraform.version from config.yaml> .
# The CLI refuses to run if the Terraform inside differs from config.yaml.

ARG PYTHON_VERSION=3.13
ARG UV_VERSION=0.11.15

# uv is copied out of its release image; `COPY --from` cannot expand variables,
# so the image gets a named stage.
FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

# --- stage 1: Terraform binary, verified against HashiCorp's checksums ---------
FROM debian:bookworm-slim AS terraform
ARG TERRAFORM_VERSION
ARG TARGETARCH
RUN test -n "$TERRAFORM_VERSION" || (echo "TERRAFORM_VERSION build-arg is required" && exit 1)
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl unzip \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /tmp/terraform
RUN curl -fsSLO "https://releases.hashicorp.com/terraform/${TERRAFORM_VERSION}/terraform_${TERRAFORM_VERSION}_linux_${TARGETARCH}.zip" \
    && curl -fsSLO "https://releases.hashicorp.com/terraform/${TERRAFORM_VERSION}/terraform_${TERRAFORM_VERSION}_SHA256SUMS" \
    && grep "terraform_${TERRAFORM_VERSION}_linux_${TARGETARCH}.zip" "terraform_${TERRAFORM_VERSION}_SHA256SUMS" | sha256sum -c - \
    && unzip -q "terraform_${TERRAFORM_VERSION}_linux_${TARGETARCH}.zip" terraform \
    && chmod 0755 terraform

# --- stage 2: the Python package and the vendored stack -------------------------
# Alpine for both stages: the virtual environment is copied between them, so
# the wheels (musl) must match. Terraform is a static binary and every
# dependency ships musl wheels. git: the registry serves GitHub-hosted modules
# as git:: sources, which terraform init clones.
FROM python:${PYTHON_VERSION}-alpine AS build
RUN apk add --no-cache ca-certificates git
COPY --from=uv /uv /uvx /usr/local/bin/
# The virtual environment is created at its final path: console-script
# shebangs are absolute, so a venv moved after creation does not start.
# No bytecode compilation: it adds tens of MB for a start-up gain nobody notices.
ENV UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/jwall-engineering-sites/venv
WORKDIR /src
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
# --no-editable: an editable install points at /src, which the runtime stage does not have.
RUN uv sync --locked --no-dev --no-editable
# botocore ships API definitions for every AWS service; the tool uses seven.
# Keep those plus botocore's shared files (endpoints, partitions, retries).
RUN cd /opt/jwall-engineering-sites/venv/lib/python3.13/site-packages/botocore/data \
    && find . -mindepth 1 -maxdepth 1 -type d ! -name s3 ! -name sts ! -name cloudfront ! -name ssm ! -name ec2 ! -name logs ! -name ecr -exec rm -rf {} + \
    && find /opt/jwall-engineering-sites/venv -type d -name __pycache__ -prune -exec rm -rf {} +

COPY --from=terraform /tmp/terraform/terraform /usr/local/bin/terraform
COPY stacks/site /opt/jwall-engineering-sites/stacks/site
COPY stacks/bot /opt/jwall-engineering-sites/stacks/bot
# Fetch the library modules and mirror the providers so run-time init is offline.
# Both stacks share one provider mirror (their lock files pin the same
# provider). The unpacked copies that `init` leaves under
# .terraform/providers are the single largest thing in the image and run time
# never reads them, so they go. The modules stay.
RUN for stack in site bot; do \
      cd "/opt/jwall-engineering-sites/stacks/$stack" \
      && terraform init -backend=false -input=false \
      && terraform providers mirror /opt/jwall-engineering-sites/plugins \
      && rm -rf .terraform/providers \
      && find .terraform/modules -type d \( -name .git -o -name examples -o -name tests -o -name docs \) -prune -exec rm -rf {} + \
      || exit 1; \
    done

# --- stage 3: the runtime image --------------------------------------------------
FROM python:${PYTHON_VERSION}-alpine
ARG TERRAFORM_VERSION
LABEL org.opencontainers.image.title="jwall-engineering-sites" \
      org.opencontainers.image.source="https://github.com/jwallace145/jwall-engineering-infra" \
      org.opencontainers.image.description="Provisions and publishes the jwall.engineering sites" \
      terraform.version="${TERRAFORM_VERSION}"
# No git at run time: the library modules are already in the image. No pip
# either: the virtual environment is complete and nothing installs at run time.
RUN apk add --no-cache ca-certificates \
    && python -m pip uninstall -y pip setuptools wheel >/dev/null 2>&1 || true \
    && rm -rf /usr/local/lib/python3.13/ensurepip /usr/local/lib/python3.13/idlelib \
       /usr/local/lib/python3.13/tkinter /usr/local/lib/python3.13/test /root/.cache
COPY --from=build /opt/jwall-engineering-sites/venv /opt/jwall-engineering-sites/venv
COPY --from=build /usr/local/bin/terraform /usr/local/bin/terraform
COPY --from=build /opt/jwall-engineering-sites/stacks /opt/jwall-engineering-sites/stacks
COPY --from=build /opt/jwall-engineering-sites/plugins /opt/jwall-engineering-sites/plugins
COPY config.yaml /etc/jwall-engineering-sites/config.yaml
ENV PATH="/opt/jwall-engineering-sites/venv/bin:${PATH}" \
    TF_IN_AUTOMATION=1 \
    TF_INPUT=0 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
# The workspace (a site repository checkout) is mounted here. Root inside the
# container so files it writes into the mounted workspace stay deletable by the
# runner; nothing in the image needs privileges.
WORKDIR /work
ENTRYPOINT ["jwall-engineering-sites"]
CMD ["--help"]
