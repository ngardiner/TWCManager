FROM python:3.9-slim

# Avoid prompts from apt
ENV DEBIAN_FRONTEND=noninteractive

# Install system dependencies
RUN apt-get update && apt-get install -y \
    curl \
    mosquitto \
    mosquitto-clients \
    mariadb-server \
    sqlite3 \
    build-essential \
    git \
    && rm -rf /var/lib/apt/lists/*

# Configure Mosquitto
RUN mkdir -p /var/log/mosquitto /var/lib/mosquitto && \
    chown mosquitto:mosquitto /var/log/mosquitto /var/lib/mosquitto

COPY tests/docker/mosquitto.conf /etc/mosquitto/mosquitto.conf

# Configure MariaDB for testing
RUN mkdir -p /var/run/mysqld && \
    chown mysql:mysql /var/run/mysqld

# Create test user and directories
RUN useradd -m -s /bin/bash twcmanager && \
    mkdir -p /etc/twcmanager/log /etc/twcmanager/csv && \
    chown -R twcmanager:twcmanager /etc/twcmanager

# Set working directory
WORKDIR /app

# Copy requirements first for better caching
COPY requirements.txt requirements-test.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-test.txt

# Copy application code
COPY . .

# Setup test environment
RUN chmod +x tests/docker/entrypoint.sh

# Expose ports
EXPOSE 8088 1883 3306

# Health check
HEALTHCHECK --interval=5s --timeout=3s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8088/api/getStatus || exit 1

ENTRYPOINT ["tests/docker/entrypoint.sh"]
CMD ["pytest", "tests/", "-v"]
