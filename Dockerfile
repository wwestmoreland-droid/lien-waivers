FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/data

WORKDIR /app
COPY pyproject.toml ./
COPY app ./app
RUN pip install --upgrade pip && pip install -e .

COPY alembic.ini docker-entrypoint.sh ./
COPY alembic ./alembic
COPY templates ./templates
RUN chmod +x docker-entrypoint.sh && mkdir -p /data

EXPOSE 8000
CMD ["./docker-entrypoint.sh"]
