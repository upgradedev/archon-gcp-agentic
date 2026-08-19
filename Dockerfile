# Cloud Run image. Slim on purpose: the whole deterministic engine is standard
# library, so the layer that matters is the one holding ADK and FastAPI.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first, so a code change does not re-resolve the whole tree.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY archon/ ./archon/
COPY corpus/ ./corpus/
COPY web/ ./web/

# Cloud Run sets PORT and expects the container to listen on it. Hard-coding
# 8080 works until it does not, which is a bad way to find out.
ENV PORT=8080
EXPOSE 8080

# One worker. The close is CPU-light and finishes in milliseconds; concurrency
# is Cloud Run's job, not gunicorn's.
CMD exec uvicorn archon.service:app --host 0.0.0.0 --port ${PORT}
