FROM python:3.12-slim

# Install LibreOffice headless for DOCX → PDF conversion
RUN apt-get update && apt-get install -y --no-install-recommends \
      libreoffice-core \
      libreoffice-writer \
      libreoffice-common \
      fonts-dejavu \
      fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend.py processing.py frontend.html ./

# Persistent data directory
ENV BASE_DIR=/data
RUN mkdir -p /data/uploads /data/processed

EXPOSE 8000

CMD ["uvicorn", "backend:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
