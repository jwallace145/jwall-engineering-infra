# jwall-engineering-sites: the CLI, a pinned Terraform, the site stack with its
# modules and providers vendored, and config.yaml. Only the S3 backend touches
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
FROM python:${PYTHON_VERSION}-slim-bookworm AS build
# git: the registry serves GitHub-hosted modules as git:: sources, which
# terraform init clones.
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates git \
    && rm -rf /var/lib/apt/lists/*
COPY --from=uv /uv /uvx /usr/local/bin/
# The virtual environment is created at its final path: console-script
# shebangs are absolute, so a venv moved after creation does not start.
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/jwall-engineering-sites/venv
WORKDIR /src
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
# --no-editable: an editable install points at /src, which the runtime stage does not have.
RUN uv sync --locked --no-dev --no-editable

COPY --from=terraform /tmp/terraform/terraform /usr/local/bin/terraform
COPY stacks/site /opt/jwall-engineering-sites/stacks/site
# Fetch the library modules and mirror the providers so run-time init is offline.
RUN cd /opt/jwall-engineering-sites/stacks/site \
    && terraform init -backend=false -input=false \
    && terraform providers mirror /opt/jwall-engineering-sites/plugins

# --- stage 3: the runtime image --------------------------------------------------
FROM python:${PYTHON_VERSION}-slim-bookworm
ARG TERRAFORM_VERSION
LABEL org.opencontainers.image.title="jwall-engineering-sites" \
      org.opencontainers.image.source="https://github.com/jwallace145/jwall-engineering-infra" \
      org.opencontainers.image.description="Provisions and publishes the jwall.engineering sites" \
      terraform.version="${TERRAFORM_VERSION}"
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates git \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 sites
COPY --from=build /opt/jwall-engineering-sites/venv /opt/jwall-engineering-sites/venv
COPY --from=build /usr/local/bin/terraform /usr/local/bin/terraform
COPY --from=build /opt/jwall-engineering-sites/stacks /opt/jwall-engineering-sites/stacks
COPY --from=build /opt/jwall-engineering-sites/plugins /opt/jwall-engineering-sites/plugins
COPY config.yaml /etc/jwall-engineering-sites/config.yaml
ENV PATH="/opt/jwall-engineering-sites/venv/bin:${PATH}" \
    TF_IN_AUTOMATION=1 \
    TF_INPUT=0 \
    TF_PLUGIN_CACHE_DIR="" \
    PYTHONUNBUFFERED=1
# The workspace (a site repository checkout) is mounted here. Root inside the
# container so files it writes into the mounted workspace stay deletable by the
# runner; nothing in the image needs privileges.
WORKDIR /work
ENTRYPOINT ["jwall-engineering-sites"]
CMD ["--help"]
