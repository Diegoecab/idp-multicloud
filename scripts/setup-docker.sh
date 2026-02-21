#!/bin/bash
# Setup script for IDP Multicloud — Docker Compose
# Fast local development with persistent storage

set -e

echo "=========================================="
echo "IDP Multicloud — Docker Setup"
echo "=========================================="

# Check that Docker is available
if ! command -v docker &> /dev/null; then
    echo "❌ Docker is not installed"
    echo "Install it from: https://docs.docker.com/get-docker/"
    exit 1
fi

if ! command -v docker-compose &> /dev/null; then
    echo "❌ Docker Compose is not installed"
    echo "Install it from: https://docs.docker.com/compose/install/"
    exit 1
fi

echo "✓ Docker and Docker Compose detected"

# Step 1: Build the image
echo ""
echo "Step 1: Building Docker image..."
docker build -t idp-controlplane:latest -f Dockerfile .
echo "✓ Image built"

# Step 2: Start services
echo ""
echo "Step 2: Starting containers..."
docker-compose up -d
echo "✓ Containers started"

# Step 3: Wait for it to be ready
echo ""
echo "Step 3: Waiting for control plane to be accessible..."
for i in {1..30}; do
    if curl -s http://localhost:8080/ > /dev/null 2>&1; then
        echo "✓ Control plane is ready"
        break
    fi
    echo "  Attempt $i/30..."
    sleep 1
done

# Step 4: Information
echo ""
echo "=========================================="
echo "✓ IDP Controlplane is running!"
echo "=========================================="
echo ""
echo "Web UI:"
echo "  http://localhost:8080/web/"
echo ""
echo "API:"
echo "  curl http://localhost:8080/api/products"
echo ""
echo "Useful commands:"
echo "  # View logs"
echo "  docker-compose logs -f idp-controlplane"
echo ""
echo "  # Check status"
echo "  docker-compose ps"
echo ""
echo "  # Access SQLite database"
echo "  docker exec -it idp-controlplane sqlite3 /data/idp.db '.tables'"
echo ""
echo "  # Stop"
echo "  docker-compose down"
echo ""
echo "  # Clean (reset database)"
echo "  docker-compose down -v"
echo "  docker-compose up -d"
echo ""
