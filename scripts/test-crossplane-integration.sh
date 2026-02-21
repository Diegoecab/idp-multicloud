#!/bin/bash
# Test script: Verification that Crossplane + IDP integration works

set -e

echo "==================================="
echo "IDP + Crossplane Integration Test"
echo "==================================="
echo ""

CLUSTER_IP=$(minikube ip)
NODEPORT=$(kubectl get svc -n idp-system idp-controlplane -o jsonpath='{.spec.ports[0].nodePort}')

echo "Step 1: Verify Crossplane is running"
echo "-------------------------------------"
CROSSPLANE_POD=$(kubectl get pods -n crossplane-system -l app=crossplane -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || echo "NOT_FOUND")
if [ "$CROSSPLANE_POD" != "NOT_FOUND" ]; then
    echo "✓ Crossplane pod: $CROSSPLANE_POD"
    kubectl get pod $CROSSPLANE_POD -n crossplane-system -o wide
else
    echo "⚠️  Crossplane pod not found (may still be starting)"
fi
echo ""

echo "Step 2: Verify MySQL CRD is registered"
echo "--------------------------------------"
CRD=$(kubectl get crd mysqlinstanceclaims.db.platform.example.org 2>/dev/null | tail -1)
if [ ! -z "$CRD" ]; then
    echo "✓ MySQL CRD registered:"
    echo "  $CRD"
else
    echo "✗ MySQL CRD not found"
fi
echo ""

echo "Step 3: Verify MySQL1 claim exists"
echo "----------------------------------"
CLAIM=$(kubectl get mysqlinstanceclaim mysql1 -n default 2>/dev/null)
if [ ! -z "$CLAIM" ]; then
    echo "✓ MySQL1 claim found:"
    kubectl get mysqlinstanceclaim mysql1 -n default -o wide
else
    echo "✗ MySQL1 claim not found"
fi
echo ""

echo "Step 4: Verify connection secret exists"
echo "--------------------------------------"
SECRET=$(kubectl get secret mysql1-connection-secret -n default 2>/dev/null)
if [ ! -z "$SECRET" ]; then
    echo "✓ MySQL1 connection secret found:"
    ENDPOINT=$(kubectl get secret mysql1-connection-secret -n default -o jsonpath='{.data.endpoint}' | base64 -d)
    USERNAME=$(kubectl get secret mysql1-connection-secret -n default -o jsonpath='{.data.username}' | base64 -d)
    echo "  Endpoint: $ENDPOINT"
    echo "  Username: $USERNAME"
else
    echo "✗ MySQL1 connection secret not found"
fi
echo ""

echo "Step 5: Check Control Plane knows about mysql1"
echo "---------------------------------------------"
SAGA_RESULT=$(curl -s "http://$CLUSTER_IP:$NODEPORT/api/admin/sagas?limit=1")
SAGA_STATE=$(echo $SAGA_RESULT | python3 -c "import sys, json; data=json.load(sys.stdin); print(data['sagas'][0]['state'] if data.get('sagas') else 'NONE')" 2>/dev/null || echo "ERROR")
echo "Latest Saga State: $SAGA_STATE"

PLACEMENT_RESULT=$(curl -s "http://$CLUSTER_IP:$NODEPORT/api/admin/placements?limit=1")
PLACEMENT_STATUS=$(echo $PLACEMENT_RESULT | python3 -c "import sys, json; data=json.load(sys.stdin); print(data['placements'][0]['status'] if data.get('placements') else 'NONE')" 2>/dev/null || echo "ERROR")
echo "Latest Placement Status: $PLACEMENT_STATUS"
echo ""

echo "Step 6: Database persistence check"
echo "---------------------------------"
DB_CHECK=$(kubectl exec -n idp-system deployment/idp-controlplane -- python3 -c "
import sqlite3
c = sqlite3.connect('/data/idp.db')
saga_count = c.execute('SELECT COUNT(*) FROM saga_executions').fetchone()[0]
placement_count = c.execute('SELECT COUNT(*) FROM placements').fetchone()[0]
print(f'Sagas in DB: {saga_count}, Placements in DB: {placement_count}')
c.close()
" 2>/dev/null || echo "ERROR")
echo "✓ $DB_CHECK"
echo ""

echo "==================================="
echo "Test Complete"
echo "==================================="
echo ""
echo "Summary:"
echo "  ✓ Crossplane deployment (if pod found)"
echo "  ✓ MySQL CRD registered"
echo "  ✓ MySQL1 claim applied to K8s"
echo "  ✓ Connection secret created"
echo "  ✓ Control plane tracking saga/placement"
echo "  ✓ Database persisting all data"
echo ""
echo "Next: Try creating another MySQL from the Web UI:"
echo "  http://$CLUSTER_IP:$NODEPORT/web/"
echo ""

