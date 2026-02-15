from internal.scheduler.scheduler import schedule_mysql


def test_c0_filters_out_oci_multiaz_gap():
    result = schedule_mysql(cell="payments", tier_name="C0", ha=True)
    excluded = {e["candidate"]: e["gateFailures"] for e in result["reason"]["excluded"]}
    assert any("oci:us-ashburn-1" in k for k in excluded.keys())


def test_c2_prefers_cost_weighting_candidate():
    result = schedule_mysql(cell="payments", tier_name="C2", ha=False)
    assert result["provider"] in {"aws", "gcp", "oci"}
    assert len(result["reason"]["top3"]) >= 1
