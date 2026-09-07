FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && groupadd --gid 1000 appuser \
    && useradd --uid 1000 --gid appuser --no-create-home appuser

COPY app/ ./app/
RUN mkdir -p data reply_examples && chown -R appuser:appuser /app
USER appuser

EXPOSE 9001
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9001/api/health', timeout=3)"

# One worker owns the SQLite webhook queue; do not scale workers or replicas.
CMD ["gunicorn", "--workers", "1", "--threads", "4", "--bind", "0.0.0.0:9001", "--access-logfile", "-", "--error-logfile", "-", "app.dashboard:create_serving_app()"]
