# WP0 spike summary

Path-loss exponent used: 4.0

| Test | Verdict | Headline |
|---|---|---|
| env | PASS | {"python_in_station": "3.14.4", "all_checks": true} |
| range | INFO | {"handheld_r90_m": 100, "edge_width_m": 10, "radio": {"txpower_dbm": 20, "nominal_range_m": 100}} |
| relay | INFO | {"relay_to_handheld_r90_m": 500, "handheld_to_relay_r90_m": 500, "symmetric": true, "radio": {"hand_txpower_dbm": 20, "relay_txpower_dbm": 48}} |
| inject | FAIL | {"loss_error_pp": [0.0, -0.08, 0.2], "joint_loss_ratio": [null, 0.96, 0.99], "delay_ms_median_at_0pct": 413.9869999999064, "netem_delay_ok": true} |
| mobility | PASS | {"r90_while_moving_m": 100, "movement_changes_reception": true, "max_diff_vs_range_curve": 0.008000000000000007, "setposition_lag_max_s": 0.001} |
| repeat | INFO | {"mean_spread": 0.0, "max_spread": 0.0} |
| scale | PASS | {"max_nodes_ok": 30, "cpus": 12, "by_size": {"5": {"cpu_pct": 3.0, "hello": 1.0, "reach": 1.0, "build_s": 19.67154918899905, "stop_s": 1.753900159998011}, "15": {"cpu_pct": 2.0, "hello": 1.0, "reach": 1.0, "build_s": 36.88134765900031, "stop_s": 2.2030693859996973}, "30": {"cpu_pct": 6.0, "hello": 0 |
| lifecycle | PASS | {"leaks": {"net_interfaces": 0, "wifi_interfaces": 0, "wmediumd_processes": 0}, "build_s_mean": 28.222893012399435, "stop_s_mean": 2.018609718600055} |

## Emulator parameters (feed these into C4 and the scenario generator)

| Parameter | Value |
|---|---|
| Path-loss exponent | 4.0 |
| Handheld effective broadcast range (r90) | 100 m (edge width 10 m) |
| Relay-class range, relay to handheld (r90) | 500 m |
| Handheld to relay (r90) | 500 m |
| Links symmetric | True |
| Mobility changes reception | True |
| Loss mechanism | receiver-side shim (independent per receiver: True) |
| Delay mechanism | shim (netem delay OK on broadcast: True) |
| Maximum node count that passes | 30 |
| Run overhead (build / stop, 10 nodes) | 28.222893012399435 |
| Emulator noise floor (mean spread across identical runs) | 0.0 |
