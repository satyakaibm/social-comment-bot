FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app

# docker CLI + compose plugin only (no daemon) -- this container talks to the
# host's Docker daemon over the bind-mounted socket to start/stop sibling
# tenant Compose projects. Same install source as terraform/startup-script.sh.
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl gnupg \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg \
    && chmod a+r /etc/apt/keyrings/docker.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian bookworm stable" \
        > /etc/apt/sources.list.d/docker.list \
    && apt-get update && apt-get install -y --no-install-recommends docker-ce-cli docker-compose-plugin \
    && rm -rf /var/lib/apt/lists/*

COPY billing/requirements.txt ./billing-requirements.txt
RUN pip install --no-cache-dir -r billing-requirements.txt

# Only app/password_policy.py is imported (by billing/cli.py, to reuse the
# same password rules as scripts/generate_dashboard_password.py) -- it has no
# external dependencies, so the bot's own requirements.txt is never needed here.
COPY app/ ./app/
COPY billing/ ./billing/

EXPOSE 9000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9000/api/health', timeout=3)"

CMD ["gunicorn", "--workers", "1", "--threads", "4", "--bind", "0.0.0.0:9000", "--access-logfile", "-", "--error-logfile", "-", "billing.app:create_serving_app()"]
