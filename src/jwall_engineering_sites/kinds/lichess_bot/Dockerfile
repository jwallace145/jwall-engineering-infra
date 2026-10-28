# The lichess-bot image jwall-engineering-sites builds for a `lichess-bot`
# project: lichess-bot at a pinned commit, the engine binary from the
# project's GitHub release (verified against its .sha256 before the build),
# the project's lichess/config.yml as a template with the token placeholder,
# and an entrypoint that renders the real config and execs lichess-bot.
#
# Build context (prepared by the CLI): Dockerfile, entrypoint.py, config.yml,
# engine/<binary>. The engine is a glibc x86-64 (or arm64) binary, so the
# base is Debian slim, not Alpine.

# --- lichess-bot at the pinned commit (the project publishes no release tags) --
FROM python:3.12-slim AS fetch
ARG LICHESS_BOT_COMMIT=afa430da25cb6d506c878d3ca2fa155be8da625d
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates git \
    && rm -rf /var/lib/apt/lists/*
RUN git clone --quiet https://github.com/lichess-bot-devs/lichess-bot /opt/lichess-bot \
    && git -C /opt/lichess-bot checkout --quiet "$LICHESS_BOT_COMMIT" \
    && rm -rf /opt/lichess-bot/.git /opt/lichess-bot/.github /opt/lichess-bot/test_bot /opt/lichess-bot/docs

# --- runtime ------------------------------------------------------------------
FROM python:3.12-slim
ARG ENGINE_BINARY
ARG VERSION
ARG SOURCE_REPOSITORY
RUN test -n "$ENGINE_BINARY" || (echo "ENGINE_BINARY build-arg is required" && exit 1)
LABEL org.opencontainers.image.source="${SOURCE_REPOSITORY}" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.description="lichess-bot running the ${ENGINE_BINARY} release ${VERSION}"
# uid/gid 1000: the instance owns the mounted records directory as 1000.
RUN groupadd --gid 1000 bot && useradd --uid 1000 --gid 1000 --create-home --shell /usr/sbin/nologin bot
COPY --from=fetch /opt/lichess-bot /opt/lichess-bot
RUN pip install --no-cache-dir --disable-pip-version-check -r /opt/lichess-bot/requirements.txt
COPY engine/ /opt/engine/
COPY config.yml /opt/lichess-bot/config.template.yml
COPY entrypoint.py /opt/entrypoint.py
RUN chmod 0755 "/opt/engine/$ENGINE_BINARY" \
    && install -d -o bot -g bot /opt/lichess-bot/game_records \
    && chown -R bot:bot /opt/lichess-bot
ENV ENGINE_BINARY=${ENGINE_BINARY} \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
USER bot
WORKDIR /opt/lichess-bot
# lichess-bot handles SIGINT itself: the first one lets the game in progress
# finish (quit_after_all_games_finish), so the reconciler sends exactly one.
STOPSIGNAL SIGINT
ENTRYPOINT ["python", "/opt/entrypoint.py"]
