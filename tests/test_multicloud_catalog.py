from internal.policy.cells import CELL_CATALOG


def test_payments_cell_has_all_required_providers():
    providers = {c.provider for c in CELL_CATALOG["payments"]}
    assert providers == {"aws", "gcp", "oci"}
