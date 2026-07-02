FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app_bunny.py .

CMD ["gunicorn", "-w", "4", "-k", "gthread", "--threads", "16", "-b", "0.0.0.0:8080", "app_bunny:app"]
