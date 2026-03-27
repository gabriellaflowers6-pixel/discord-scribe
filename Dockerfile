FROM python:3.11-slim

# Install system dependencies for voice recording + Whisper
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libopus0 \
    libopus-dev \
    libsodium23 \
    git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Pre-download Whisper model so first meeting doesn't have extra delay
RUN python -c "import whisper; whisper.load_model('base')"

# Copy bot code
COPY bot.py .

# Create directories the bot expects
RUN mkdir -p recordings inbox

# Run the bot
CMD ["python", "bot.py"]
