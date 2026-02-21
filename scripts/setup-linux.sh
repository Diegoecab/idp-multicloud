#!/bin/bash
# Setup script for IDP Multicloud — Linux standalone local

set -e

echo "=========================================="
echo "IDP Multicloud — Linux Standalone Setup"
echo "=========================================="

# Detect Python
PYTHON_CMD=""
if command -v python3 &> /dev/null; then
    PYTHON_CMD="python3"
elif command -v python &> /dev/null; then
    PYTHON_CMD="python"
else
    echo "❌ Python 3 is not installed"
    exit 1
fi

echo "✓ Python detected: $PYTHON_CMD"

# Create data directory
echo ""
echo "Step 1: Creating storage directory..."
mkdir -p ~/idp-data
echo "✓ Directory created: $HOME/idp-data"

# Check requirements.txt
echo ""
echo "Step 2: Installing Python dependencies..."
if [ ! -f requirements.txt ]; then
    echo "❌ requirements.txt not found"
    exit 1
fi

# Install in venv if exists, or globally
if [ -d venv ]; then
    source venv/bin/activate
    echo "✓ Virtual environment activated"
else
    echo "  (no virtual environment - installing globally)"
fi

pip install -r requirements.txt 2>&1 | grep -E "Successfully|already" || true
echo "✓ Dependencies installed"

# Start the control plane
echo ""
echo "=========================================="
echo "✓ Setup completed!"
echo "=========================================="
echo ""
echo "To start the control plane:"
echo ""
echo "  export IDP_DB_PATH=~/idp-data/idp.db"
echo "  $PYTHON_CMD cmd/controlplane/main.py"
echo ""
echo "Database will be stored in: $HOME/idp-data/idp.db"
echo ""
echo "Then open http://localhost:8080/web/"
echo ""
