# NiftyWhale — arm64/amd64 image for the Raspberry Pi homelab.
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl tzdata \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Requirements first, so a code edit does not rebuild the pandas/numpy wheels.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# /data holds the sqlite DB and the universe file; see docker-compose.yml.
ENV DB_PATH=/data/niftywhale.db \
    UNIVERSE_PATH=/data/universe.json \
    PORT=5058 \
    PYTHONUNBUFFERED=1

RUN useradd --create-home --uid 1000 whale \
 && mkdir -p /data && chown -R whale:whale /app /data
USER whale

EXPOSE 5058
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD curl -fsS http://127.0.0.1:5058/healthz >/dev/null || exit 1

ENTRYPOINT ["/app/scripts/docker-entrypoint.sh"]

# One worker: the scheduler and the scan run as threads in this process and
# share its locks; a second worker would run a second scheduler. Each open page's
# WebSocket holds a thread (at most hub.MAX_CLIENTS = 12), so there are 24.
CMD ["gunicorn", "--bind", "0.0.0.0:5058", \
     "--workers", "1", "--threads", "24", \
     "--timeout", "120", "--graceful-timeout", "30", \
     "--access-logfile", "-", "--error-logfile", "-", \
     "app:app"]
