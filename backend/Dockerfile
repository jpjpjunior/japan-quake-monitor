# Build context is the repository root (CI sets context: .).
# The frontend (nginx) and backend (FastAPI) images share this context;
# each Dockerfile copies only what it needs.
FROM python:3.12-slim

RUN useradd --uid 1000 --create-home app
WORKDIR /app

COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/main.py .
COPY index.html static/index.html
# the alert chime is a separate asset now (no longer inlined in index.html)
COPY assets/ static/assets/

ENV DATA_DIR=/data \
    STATIC_DIR=/app/static \
    PORT=8000

RUN mkdir -p /data && chown -R app:app /data /app
USER app
EXPOSE 8000
CMD ["python", "main.py"]
