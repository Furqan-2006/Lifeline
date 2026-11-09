# WP0 follow-up summary

Path-loss exponent used: 4.0

| Test | Open point | Verdict | Headline |
|---|---|---|---|
| f1_shim_delay | OP1 | PASS | {"shim_median_by_rate": {"50.0": 20.10599999994156, "20.0": 20.183999999972002, "333.0": 20.117999999911262}, "e2e_median_by_rate": {"50.0": 29.510999999729393, "20.0": 28.827000000092085, "333.0": 1515.3514999999516}, "received_by_rate": {"50.0": 1.0, "20.0": 1.0, "333.0": 1.0}, "realistic_ok": tru |
| f2_relay_edge | OP2 | PASS | {"handheld_to_relay_r90_m": 520, "handheld_to_relay_r10_m": 530, "relay_to_handheld_r90_m": 520, "relay_to_handheld_r10_m": 530, "symmetric": true, "non_monotonic": [], "monotonicity_ok": true, "edge_beyond_max_h2r": false, "edge_beyond_max_r2h": false, "interpretation": "measured_threshold_bracket_ |
| f3_handheld_txpower | OP3 | FAIL | {"expected_configured_handheld_dbm": 20.0, "measured_configured_handheld_dbm": [{"at_start": 20.0, "during": 20.0}, {"at_start": 20.0, "during": 20.0}, {"at_start": 20.0, "during": 20.0}, {"at_start": 20.0, "during": 20.0}], "handheld_to_relay_at_500m": 1.0, "qualifier": "iw reports the configured i |
| f4_scale_20 | OP4 | PASS | {"nodes": 20, "hello_delivery_ratio": 0.9998281786941581, "flood_reach_mean": 1.0, "cpu_busy_pct": 2.61052845425227, "margin_ok": true, "final_pass": true} |
| f5_startup_loss | OP5 | FAIL | {"plain_near_mean": 0.995, "drain_near_mean": 0.989, "recovered": false, "no_difference": false, "far_ok": true, "drained_total": 0, "diagnosis": "inconclusive", "qualifier": "drain phase cannot consume test packets; this measures stabilization, not buffer loss"} |
