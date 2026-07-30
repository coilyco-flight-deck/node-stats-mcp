# Single-stage: this is a tiny psutil + FastMCP service, no frontend, no build
# step. uv installs the pinned deps into the system env, then the console
# script runs the streamable-HTTP server. Mirrors eco-app's registry flow
# (in-cluster registry, plain build), minus the multi-stage frontend.
FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app
COPY pyproject.toml uv.lock README.md /app/
COPY src /app/src

# Install the exact dependency set exercised by CI. The image stays
# self-contained under /app/.venv and exposes the project console scripts
# through PATH.
RUN uv sync --frozen --no-dev --no-editable

ENV PORT=8080
ENV PATH="/app/.venv/bin:${PATH}"
EXPOSE 8080

CMD ["node-stats-mcp"]
