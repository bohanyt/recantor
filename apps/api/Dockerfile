FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN pip install --no-cache-dir uv==0.10.0

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
COPY alembic.ini ./
COPY alembic ./alembic

RUN uv sync --no-dev
ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8000
CMD ["uvicorn", "recantor.main:app", "--host", "0.0.0.0", "--port", "8000"]
