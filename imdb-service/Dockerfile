ARG BASE_IMAGE=ghcr.io/kometa-team/imdb-service-base:latest
FROM ${BASE_IMAGE}

WORKDIR /app

ENV PYTHONUNBUFFERED=1

COPY main.py importer.py charts.py constraints.py singleflight.py http_clients.py ./

RUN mkdir -p /app/data

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
