ARG BASE_DEPS=deps

FROM python:3.12-slim-bookworm@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337 AS deps

COPY --from=ghcr.io/astral-sh/uv:0.13.7@sha256:96f40d3904aad8d0bd010539001d36c8e2188b981e1334471a90c4a1d93bb490 /uv /uvx /usr/local/bin/

RUN groupadd -g 10001 labagent \
    && useradd -u 10001 -g 10001 -m -d /home/labagent -s /usr/sbin/nologin labagent

WORKDIR /opt/lab-agent/backend
COPY backend/pyproject.toml backend/uv.lock /opt/lab-agent/backend/
RUN mkdir -p app evals \
    && touch README.md app/__init__.py evals/__init__.py \
    && UV_NO_CACHE=1 uv sync --frozen --no-dev \
    && rm -f /usr/local/bin/uv /usr/local/bin/uvx \
    && mkdir -p /app/data/uploads /app/data/calibrations /run/lab-agent-secrets \
    && chown -R 10001:10001 /app/data /opt/lab-agent /home/labagent /run/lab-agent-secrets \
    && chmod 0700 /run/lab-agent-secrets

FROM ${BASE_DEPS} AS runtime

RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/* /usr/local/lib/python3.12/site-packages/pip* /usr/local/lib/python3.12/site-packages/setuptools* /usr/local/lib/python3.12/site-packages/wheel*

WORKDIR /opt/lab-agent
COPY --chown=10001:10001 backend/alembic.ini /opt/lab-agent/backend/
COPY --chown=10001:10001 README.md /opt/lab-agent/backend/README.md
COPY --chown=10001:10001 backend/app /opt/lab-agent/backend/app
COPY --chown=10001:10001 backend/evals /opt/lab-agent/backend/evals
COPY --chown=10001:10001 backend/alembic /opt/lab-agent/backend/alembic
COPY --chown=10001:10001 evals /opt/lab-agent/evals
COPY --chmod=0755 scripts/api-entrypoint.sh /usr/local/bin/api-entrypoint.sh

WORKDIR /opt/lab-agent/backend
ENV PATH="/opt/lab-agent/backend/.venv/bin:${PATH}"
ENV PYTHONUNBUFFERED=1
USER 10001:10001
EXPOSE 8000
CMD ["api-entrypoint.sh"]
