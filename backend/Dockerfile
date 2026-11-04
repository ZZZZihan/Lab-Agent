FROM python:3.12-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /uvx /usr/local/bin/

WORKDIR /opt/yanzheng
COPY backend/pyproject.toml backend/uv.lock backend/alembic.ini /opt/yanzheng/backend/
COPY backend/app /opt/yanzheng/backend/app
COPY backend/evals /opt/yanzheng/backend/evals
COPY backend/alembic /opt/yanzheng/backend/alembic
COPY evals /opt/yanzheng/evals
COPY scripts/api-entrypoint.sh /usr/local/bin/api-entrypoint.sh
COPY README.md /opt/yanzheng/backend/README.md

WORKDIR /opt/yanzheng/backend
RUN uv sync --frozen --no-dev \
    && chmod +x /usr/local/bin/api-entrypoint.sh \
    && mkdir -p /app/data/uploads

ENV PATH="/opt/yanzheng/backend/.venv/bin:${PATH}"
ENV PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["api-entrypoint.sh"]
