FROM python:3.12-slim

EXPOSE 5000

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Single worker: the official flow keeps its single-use authorization state
# in process memory, so state must not be split across workers.
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "1", "--threads", "4", "mal_oauth.app:app"]
