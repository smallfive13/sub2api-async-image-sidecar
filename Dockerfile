FROM python:3.12-alpine

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY async_image_sidecar.py /app/async_image_sidecar.py

EXPOSE 8090

CMD ["python", "/app/async_image_sidecar.py"]
