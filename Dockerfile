FROM python:3.11-slim-bookworm

# Install cec-client and cec-ctl dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        cec-utils \
        v4l-utils \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements first (layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Note: src/ will be mounted as a volume via docker-compose.yml

# Set Python to run in unbuffered mode (important for logging)
ENV PYTHONUNBUFFERED=1

# Run the application
CMD ["python", "-u", "-m", "src.main"]
