ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.12.12
ARG PYTHON_BUILD_IMAGE=dhi.io/python:3.14-debian13-dev
ARG PYTHON_RUNTIME_IMAGE=dhi.io/python:3.14-debian13
FROM ${UV_IMAGE} AS uv
FROM ${PYTHON_BUILD_IMAGE} AS builder
USER 0
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./
COPY src ./src
RUN --mount=type=secret,id=enterprise_ca \
    set -eu; \
    if [ -f /run/secrets/enterprise_ca ]; then \
      cat /etc/ssl/certs/ca-certificates.crt /run/secrets/enterprise_ca > /tmp/build-ca.pem; \
      export SSL_CERT_FILE=/tmp/build-ca.pem; \
    fi; \
    uv sync --locked --no-dev --no-editable --python python3.14 --no-managed-python; \
    /app/.venv/bin/python -c 'import sys; assert sys.version_info[:2] == (3, 14)'; \
    rm -f /tmp/build-ca.pem

FROM ${PYTHON_RUNTIME_IMAGE} AS runtime
LABEL org.opencontainers.image.source="https://github.com/lewisco/ms-graph-mcp"
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
USER 65532:65532
EXPOSE 8000
ENTRYPOINT ["/app/.venv/bin/python", "-m", "uvicorn"]
CMD ["ms_graph_mcp.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-access-log", "--no-proxy-headers"]
