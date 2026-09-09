FROM python:3.12-slim

# Short git SHA of the build, surfaced by /healthz so a deploy can confirm the
# image it just pushed is the one actually serving traffic.
ARG GIT_SHA=dev
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_VERSION=$GIT_SHA

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY ad.py wsgi.py ./

# Run as an unprivileged user. Create the log dir and hand it to that user so
# the app can still write LOG_FILE when one is configured.
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/logs \
    && chown -R appuser:appuser /app/logs
USER appuser

EXPOSE 5000

# Liveness check against the dependency-free /healthz endpoint (no curl in slim).
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:5000/healthz', timeout=3).status==200 else 1)"

CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "4", "wsgi:app"]
