FROM python:3.15-slim-bookworm@sha256:96599df822d0e1c6bb700014a05c7f3c8cabc6e0f7ff3c4f9f69cee033b4b5d9 AS deps

COPY --from=ghcr.io/astral-sh/uv:0.13.7@sha256:96f40d3904aad8d0bd010539001d36c8e2188b981e1334471a90c4a1d93bb490 /uv /uvx /usr/local/bin/

WORKDIR /opt/lab-agent/backend
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

FROM python:3.15-slim-bookworm@sha256:96599df822d0e1c6bb700014a05c7f3c8cabc6e0f7ff3c4f9f69cee033b4b5d9

COPY --from=ghcr.io/astral-sh/uv:0.13.7@sha256:96f40d3904aad8d0bd010539001d36c8e2188b981e1334471a90c4a1d93bb490 /uv /uvx /usr/local/bin/

RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/* /usr/local/lib/python3.12/site-packages/pip* /usr/local/lib/python3.12/site-packages/setuptools* /usr/local/lib/python3.12/site-packages/wheel* \
    && groupadd -g 10001 labagent \
    && useradd -u 10001 -g 10001 -m -d /home/labagent -s /usr/sbin/nologin labagent

COPY --from=deps /opt/lab-agent/backend/.venv /opt/lab-agent/backend/.venv

WORKDIR /opt/lab-agent
COPY backend/pyproject.toml backend/uv.lock backend/alembic.ini /opt/lab-agent/backend/
COPY backend/app /opt/lab-agent/backend/app
COPY backend/evals /opt/lab-agent/backend/evals
COPY backend/alembic /opt/lab-agent/backend/alembic
COPY evals /opt/lab-agent/evals
COPY scripts/api-entrypoint.sh /usr/local/bin/api-entrypoint.sh
COPY README.md /opt/lab-agent/backend/README.md

WORKDIR /opt/lab-agent/backend
RUN uv sync --frozen --no-dev \
    && chmod +x /usr/local/bin/api-entrypoint.sh \
    && mkdir -p /app/data/uploads /app/data/calibrations /run/lab-agent-secrets \
    && chown -R 10001:10001 /app/data /opt/lab-agent /home/labagent /run/lab-agent-secrets \
    && chmod 0700 /run/lab-agent-secrets

ENV PATH="/opt/lab-agent/backend/.venv/bin:${PATH}"
ENV PYTHONUNBUFFERED=1
USER 10001:10001
EXPOSE 8000
CMD ["api-entrypoint.sh"]
