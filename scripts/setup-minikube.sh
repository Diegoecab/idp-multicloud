#!/bin/bash
# Setup script for IDP Multicloud on Minikube

set -e

echo "=========================================="
echo "IDP Multicloud — Minikube Setup"
echo "=========================================="

# Check that Minikube is installed
if ! command -v minikube &> /dev/null; then
    echo "❌ Minikube is not installed"
    echo "Install it from: https://minikube.sigs.k8s.io/docs/start/"
    exit 1
fi

# Check that Docker is installed
if ! command -v docker &> /dev/null; then
    echo "❌ Docker is not installed"
    echo "Install it from: https://docs.docker.com/get-docker/"
    exit 1
fi

echo "✓ Minikube and Docker detected"

# Step 1: Start Minikube (if not running)
echo ""
echo "Step 1: Starting Minikube..."
if ! minikube status &> /dev/null; then
    # Using --force because we're running as root in a container environment
    minikube start --cpus=2 --memory=4096 --driver=docker --force
    echo "✓ Minikube started"
else
    echo "✓ Minikube already running"
fi

# Step 2: Point Docker to Minikube Docker daemon
echo ""
echo "Step 2: Configuring Docker to use Minikube..."
eval $(minikube docker-env)
echo "✓ Docker connected to Minikube"

# Step 3: Build image inside Minikube
echo ""
echo "Step 3: Building Docker image in Minikube..."
docker build -t idp-controlplane:latest -f Dockerfile .
echo "✓ Image built: idp-controlplane:latest"

# Step 4: Create namespace and PV
echo ""
echo "Step 4: Creating persistent storage resources..."
# Create namespace first
kubectl create namespace idp-system --dry-run=client -o yaml | kubectl apply -f -
# Then apply the manifest
kubectl apply -f manifests/controlplane-k8s.yaml
echo "✓ Namespace and resources created"

# Step 5: Wait for Pod to be ready
echo ""
echo "Step 5: Waiting for Pod to be ready..."
kubectl wait --for=condition=ready pod \
    -l app=idp-controlplane \
    -n idp-system \
    --timeout=120s || true

# Step 6: Get access URL
echo ""
echo "Step 6: Setting up access..."
echo ""
echo "=========================================="
echo "✓ IDP Controlplane is running!"
echo "=========================================="
echo ""
echo "Access via port-forward:"
echo "  kubectl port-forward -n idp-system svc/idp-controlplane 8080:8080"
kubectl port-forward -n idp-system svc/idp-controlplane 8080:8080
echo ""
echo "Then open in your browser:"
echo "  http://localhost:8080/web/"
echo ""
echo "Or use Minikube Service:"
echo minikube service idp-controlplane -n idp-system 

# Step 7: Useful commands
echo ""
echo "Useful commands:"
echo "  # View logs from the control plane"
echo "  kubectl logs -f -n idp-system deployment/idp-controlplane"
echo ""
echo "  # Check Pod status"
echo "  kubectl get pods -n idp-system"
echo ""
echo "  # Access SQLite database"
echo "  kubectl exec -it -n idp-system deployment/idp-controlplane -- sqlite3 /data/idp.db '.tables'"
echo ""
echo "  # Clean up (delete deployment)"
echo "  kubectl delete -f manifests/controlplane-k8s.yaml"
echo ""
echo "  # Stop Minikube"
echo "  minikube stop"
echo ""
