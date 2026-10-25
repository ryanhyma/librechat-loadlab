# loadlab — LibreChat capacity & user-error harness
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && useradd --create-home --uid 10001 loadlab

COPY loadlab ./loadlab
COPY docs ./docs
COPY scripts ./scripts

RUN mkdir -p /app/runs && chown -R loadlab:loadlab /app
USER loadlab

EXPOSE 8090 8099
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8090/api/health',timeout=3)"

CMD ["python", "-m", "loadlab.cli", "serve", "--host", "0.0.0.0", "--port", "8090"]
