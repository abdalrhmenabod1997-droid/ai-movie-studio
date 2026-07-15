FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

ENV PORT=8000
# NOTE: rate limiting uses an in-memory store, which only tracks requests seen by *this* worker
# process. Increasing WEB_CONCURRENCY beyond 1 effectively multiplies the configured limits
# (2 workers ~= 2x the limit). See DEPLOY.md before scaling to multiple workers/instances.
ENV WEB_CONCURRENCY=1
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --workers ${WEB_CONCURRENCY}"]
