from internal.policy.types import Candidate


CELL_CATALOG = {
    "payments": [
        Candidate(
            provider="aws",
            region="us-east-1",
            runtime_cluster="eks-payments-use1-primary",
            network={"vpcId": "vpc-aws-payments-use1", "subnetGroup": "private-db", "securityGroup": "sg-db-private"},
            capabilities={"pitr": True, "multiaz": True, "privateNetworking": True},
            scores={"latency": 93, "dr": 91, "maturity": 95, "cost": 62},
        ),
        Candidate(
            provider="gcp",
            region="us-central1",
            runtime_cluster="gke-payments-usc1-primary",
            network={"vpc": "vpc-gcp-payments-usc1", "subnetwork": "db-private", "authorizedNetworkTag": "db-private"},
            capabilities={"pitr": True, "multiaz": True, "privateNetworking": True},
            scores={"latency": 88, "dr": 90, "maturity": 92, "cost": 74},
        ),
        Candidate(
            provider="oci",
            region="us-ashburn-1",
            runtime_cluster="oke-payments-iad-primary",
            network={"vcnOcid": "ocid1.vcn.oc1.iad.payments", "subnetOcid": "ocid1.subnet.oc1.iad.dbprivate", "nsgOcid": "ocid1.nsg.oc1.iad.dbprivate"},
            capabilities={"pitr": True, "multiaz": False, "privateNetworking": True},
            scores={"latency": 80, "dr": 70, "maturity": 75, "cost": 85},
        ),
    ]
}
