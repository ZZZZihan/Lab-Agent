FROM python:3.12-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /uvx /usr/local/bin/

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
    && mkdir -p /app/data/uploads

ENV PATH="/opt/lab-agent/backend/.venv/bin:${PATH}"
ENV PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["api-entrypoint.sh"]
