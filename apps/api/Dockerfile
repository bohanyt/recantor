FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN set -eux; \
    for sources_file in /etc/apt/sources.list.d/debian.sources /etc/apt/sources.list; do \
        if [ -f "$sources_file" ]; then \
            sed -i 's|http://deb\.debian\.org|https://deb.debian.org|g' "$sources_file"; \
        fi; \
    done; \
    apt-get -o Acquire::Retries=3 update; \
    apt-get -o Acquire::Retries=3 install -y --no-install-recommends ffmpeg; \
    ffmpeg -version >/dev/null; \
    ffprobe -version >/dev/null; \
    rm -rf /var/lib/apt/lists/*; \
    pip install --no-cache-dir uv==0.10.0

WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY src ./src
COPY alembic.ini ./
COPY alembic ./alembic

RUN uv sync --frozen --no-dev
ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8000
CMD ["uvicorn", "recantor.main:app", "--host", "0.0.0.0", "--port", "8000"]
