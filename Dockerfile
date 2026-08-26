FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    V6_DATA_DIR=/data

WORKDIR /app

COPY requirements.txt requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock

COPY . .
RUN chmod +x /app/cloud_start.sh && mkdir -p /data

EXPOSE 8501
CMD ["/app/cloud_start.sh"]
