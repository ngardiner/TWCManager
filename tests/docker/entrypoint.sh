#!/bin/bash
set -e

echo "Starting test environment services..."

# Start Mosquitto in background
echo "Starting Mosquitto MQTT broker..."
mosquitto -c /etc/mosquitto/mosquitto.conf -d

# Start MySQL in background
echo "Starting MySQL server..."
mkdir -p /var/run/mysqld
chown mysql:mysql /var/run/mysqld
mysqld --user=mysql --datadir=/var/lib/mysql --skip-networking=0 &
MYSQL_PID=$!

# Wait for MySQL to be ready
echo "Waiting for MySQL to be ready..."
for i in {1..30}; do
    if mysqladmin ping -h localhost --silent; then
        echo "MySQL is ready!"
        break
    fi
    if [ $i -eq 30 ]; then
        echo "MySQL failed to start"
        exit 1
    fi
    sleep 1
done

# Setup MySQL database and user
echo "Setting up MySQL database..."
mysql -u root <<EOF
CREATE DATABASE IF NOT EXISTS twcmanager;
CREATE USER IF NOT EXISTS 'twcmanager'@'localhost' IDENTIFIED BY 'twcmanager';
GRANT ALL PRIVILEGES ON twcmanager.* TO 'twcmanager'@'localhost';
FLUSH PRIVILEGES;

USE twcmanager;

CREATE TABLE IF NOT EXISTS charge_sessions (
    id INT AUTO_INCREMENT PRIMARY KEY,
    startTime DATETIME,
    startkWh FLOAT,
    endTime DATETIME,
    endkWh FLOAT,
    vehicleVIN VARCHAR(17),
    slaveTWC VARCHAR(4)
);

CREATE TABLE IF NOT EXISTS green_energy (
    time DATETIME PRIMARY KEY,
    genW FLOAT,
    conW FLOAT
);

CREATE TABLE IF NOT EXISTS slave_status (
    time DATETIME,
    slaveTWC VARCHAR(4),
    kWh FLOAT,
    voltsPhaseA INT,
    voltsPhaseB INT,
    voltsPhaseC INT,
    PRIMARY KEY (time, slaveTWC)
);
EOF

# Setup SQLite database
echo "Setting up SQLite database..."
sudo -u twcmanager sqlite3 /etc/twcmanager/twcmanager.sqlite <<EOF
CREATE TABLE IF NOT EXISTS charge_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    startTime TEXT,
    startkWh REAL,
    endTime TEXT,
    endkWh REAL,
    vehicleVIN TEXT,
    slaveTWC TEXT
);

CREATE TABLE IF NOT EXISTS green_energy (
    time TEXT PRIMARY KEY,
    genW REAL,
    conW REAL
);

CREATE TABLE IF NOT EXISTS slave_status (
    time TEXT,
    slaveTWC TEXT,
    kWh REAL,
    voltsPhaseA INTEGER,
    voltsPhaseB INTEGER,
    voltsPhaseC INTEGER,
    PRIMARY KEY (time, slaveTWC)
);
EOF

# Setup Mosquitto user
echo "Setting up MQTT authentication..."
mosquitto_passwd -b -c /etc/mosquitto/passwd twcmanager twcmanager
mosquitto -c /etc/mosquitto/mosquitto.conf -d

# Wait a moment for services to stabilize
sleep 2

echo "Test environment ready!"

# Execute the command passed to the container
exec "$@"
