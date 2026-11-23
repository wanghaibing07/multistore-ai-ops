FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY backend/ ./backend/
COPY frontend/dist/ ./frontend/dist/

RUN python -m pip install --no-cache-dir . \
    && groupadd --system --gid 10001 plan2 \
    && useradd --system --uid 10001 --gid plan2 --shell /usr/sbin/nologin plan2 \
    && mkdir -p /data \
    && chown -R plan2:plan2 /data

USER plan2
EXPOSE 8000

CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
