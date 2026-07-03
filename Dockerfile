# Single-stage: this is a tiny psutil + FastMCP service, no frontend, no build
# step. uv installs the pinned deps into the system env, then the console
# script runs the streamable-HTTP server. Mirrors eco-app's registry flow
# (in-cluster registry, plain build), minus the multi-stage frontend.
FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app
COPY pyproject.toml README.md /app/
COPY src /app/src

# Install the project (and its deps) into the system environment. No lockfile:
# the dependency surface is two libraries, so a resolved install is enough.
RUN uv pip install --system --no-cache .

ENV PORT=8080
EXPOSE 8080

CMD ["node-stats-mcp"]
