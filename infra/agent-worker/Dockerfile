# Agent worker: the broker's agent lane + the OFFICIAL Claude Code and Codex CLIs, in its own container.
# Built ON TOP of the broker image (BASE_IMAGE) so the Python deps are not duplicated:
#   docker compose build api && docker compose --profile agent build agent-worker
# Node 22 comes from the official node:22-slim image; both CLIs are pinned to exact versions
# (claude >= 2.1.277, the wrap-up allowance release). Bump deliberately: staged build, smoke
# (`claude --version`, `codex --version`), rollback = previous tag. NO login, NO credentials are
# baked in. Codex ships its native binary through optionalDependencies: never --omit=optional.
ARG BASE_IMAGE=aibroker-api:latest
FROM node:22-slim AS node

FROM ${BASE_IMAGE}
USER root
ARG CLAUDE_CODE_VERSION=2.1.296
ARG CODEX_VERSION=0.162.1
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && npm install -g @anthropic-ai/claude-code@${CLAUDE_CODE_VERSION} \
    && npm install -g @openai/codex@${CODEX_VERSION} \
    && npm cache clean --force \
    && claude --version \
    && codex --version
# CLI auth state: one directory per CLI, each a named volume (never an image layer).
ENV CLAUDE_CONFIG_DIR=/home/app/claude-state \
    CODEX_HOME=/home/app/codex-state \
    DISABLE_AUTOUPDATER=1 \
    HOME=/home/app/claude-state \
    AGENT_WORKSPACE_ROOT=/var/lib/aibroker/agent-workspaces
# Marker the agent lane checks before it starts (refuses to run outside this image).
RUN echo agent-worker > /etc/aibroker-agent-worker \
    && mkdir -p /home/app/claude-state /home/app/codex-state /var/lib/aibroker/agent-workspaces \
    && chown -R app:app /home/app /var/lib/aibroker
USER app
# The base image's HEALTHCHECK curls :8000, which this worker never binds.
HEALTHCHECK NONE
CMD ["python", "-m", "aibroker.agent.run_worker"]
