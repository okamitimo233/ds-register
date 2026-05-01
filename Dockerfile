FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    OPENAI_POOL_LISTEN_HOST=0.0.0.0 \
    OPENAI_POOL_LISTEN_PORT=18421

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --upgrade pip \
    && pip install -r requirements.txt

COPY core ./core
COPY static ./static
COPY main.py ./
COPY import_tokens_to_sqlite.py ./
COPY pyproject.toml ./

RUN mkdir -p /app/data /app/data/tokens /app/data/logs

VOLUME ["/app/data"]

EXPOSE 18421

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen(f\"http://127.0.0.1:{os.getenv('OPENAI_POOL_LISTEN_PORT', '18421')}/api/status\", timeout=3).read()"]

CMD ["python", "main.py"]
