# Hosted backend for the mope.io local stack (accounts, master, regions) -- see
# mope-april-23-2021/DEPLOY_SEVALLA.md. The archived game client is not included.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8080

WORKDIR /app
COPY mope-april-23-2021/requirements-cloud.txt ./
RUN pip install -r requirements-cloud.txt

COPY mope-april-23-2021/ ./
# Account database lives here; mount a persistent disk at /app/data.
RUN mkdir -p /app/data /app/logs /app/run

EXPOSE 8080
CMD ["python", "cloud.py"]
