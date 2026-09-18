# bioconice-mcp: the MCP server (src/bioconice/mcp.py) as a docker-compose
# service on onclappc02, behind Traefik. See mcp/README.md for the deploy
# steps and mcp/docker-compose.yml for the Traefik wiring.
#
# Build context is the repo root (mcp/docker-compose.yml sets `context: ..`),
# because the package lives at src/bioconice, not under mcp/.
FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir uv

COPY pyproject.toml uv.lock ./
COPY src ./src

# --frozen: the committed uv.lock is the reproducible build, never re-resolved
# at image-build time. --no-dev: pytest etc. have no place in the runtime image.
RUN uv sync --frozen --no-dev

# Pre-install DuckDB's iceberg/httpfs extensions at build time (not just
# LOAD, INSTALL fetches the .duckdb_extension binary) so a cold container
# start reads the lake immediately instead of first fetching two extensions
# from extensions.duckdb.org.
RUN uv run python -c "import duckdb; c = duckdb.connect(); c.execute('INSTALL iceberg; INSTALL httpfs;')"

ENV PORT=8000
EXPOSE 8000

# ponytail: runs as root (python:3.12-slim's default) — v1 is read-only,
# unauthenticated, public data, so there is nothing a non-root user buys
# here; add a non-root user if this image ever mounts a volume or gains a
# write path.
CMD ["uv", "run", "bioconice-mcp", "--http"]
