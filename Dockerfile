FROM python:3.11-slim

# ffmpeg + ffprobe are required for conversion; ca-certificates for HTTPS leech
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg ca-certificates && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .

# Health check endpoint used by Render / other hosts
ENV PORT=8080
EXPOSE 8080

# Secrets are injected at runtime via environment variables:
# API_ID, API_HASH, BOT_TOKEN, MONGO_URI — do not bake them into the image.
CMD ["python", "bot.py"]
