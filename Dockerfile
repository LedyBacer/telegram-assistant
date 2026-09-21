FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install .

COPY alembic.ini ./
COPY alembic ./alembic
COPY miniapp ./miniapp

RUN useradd --create-home appuser \
    && mkdir -p /data/storage/files \
    && chown -R appuser:appuser /app /data
USER appuser
ENV HOME=/home/appuser \
    STORAGE_DIR=/data/storage

EXPOSE 8000

CMD ["python", "-m", "assistant.api.main"]
