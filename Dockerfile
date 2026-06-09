FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py schema.sql climate_normals.json ./
COPY templates ./templates
COPY static ./static

ENV WEB_HOST=0.0.0.0 \
    WEB_PORT=5000 \
    DB_PATH=/data/ttn_data.db \
    FEEDS_PATH=/config/feeds.json \
    BACKUP_DIR=/data/backups \
    LOG_FILE=/data/app.log

EXPOSE 5000
VOLUME ["/data", "/config"]

CMD ["python", "app.py"]
