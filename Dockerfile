FROM python:3.11-slim-bookworm

# The service talks to /dev/cec0 directly; v4l-utils only provides cec-ctl
# for debugging (docker exec mqtt_cec_controller cec-ctl -S)
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        v4l-utils \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements first (layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Note: src/ will be mounted as a volume via docker-compose.yml

# Set Python to run in unbuffered mode (important for logging), and don't
# litter the bind-mounted src/ with root-owned __pycache__ directories
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Run the application
CMD ["python", "-u", "-m", "src.main"]
