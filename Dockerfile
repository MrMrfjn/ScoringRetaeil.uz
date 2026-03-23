FROM python:3.11-slim

RUN apt-get update && apt-get install -y gcc libsqlite3-dev curl \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

RUN useradd -m -u 1000 ccc && \
    mkdir -p /app /data/db /data/uploads /data/exports /data/backups /data/logs && \
    chown -R ccc:ccc /app /data

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

COPY --chown=ccc:ccc . .

USER ccc

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=45s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

CMD ["gunicorn", \
     "--bind", "0.0.0.0:8000", \
     "--workers", "4", \
     "--timeout", "120", \
     "--keepalive", "5", \
     "--max-requests", "1000", \
     "--max-requests-jitter", "100", \
     "--log-level", "info", \
     "--access-logfile", "-", \
     "--error-logfile", "-", \
     "main:app"]
