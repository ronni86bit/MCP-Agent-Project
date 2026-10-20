# Benchmark summary

**Runs: 7** — MIXED revision states across runs — compare within, not across: gate2=166b1625(clean); gate2-second=166b1625(clean); gate5-smoke=bdecbea4(dirty); gate5-model=bdecbea4(dirty); gate5-anti=bdecbea4(dirty); gate5-model-r2=bdecbea4(dirty); gate5-anti-r2=bdecbea4(dirty)

| run | suite | cases | pass | pass× | pass (excl. infra) | infra | p50 ms | p95 ms | tokens (p/c) | commit | dirty |
|---|---|---|---|---|---|---|---|---|---|---|---|
| gate2 | deterministic | 11 | 11 | 100.0% | 100.0% | 0 | 5.5 | 1759.9 | None/None (+11 missing) | 166b1625 | no |
| gate2-second | deterministic | 11 | 11 | 100.0% | 100.0% | 0 | 5.4 | 1694.8 | None/None (+11 missing) | 166b1625 | no |
| gate5-smoke | deterministic | 11 | 11 | 100.0% | 100.0% | 0 | 6.6 | 1792.1 | None/None (+11 missing) | bdecbea4 | yes |
| gate5-model | model | 4 | 2 | 50.0% | 50.0% | 0 | 8760.7 | 113382.9 | 36292/690 (+2 missing) | bdecbea4 | yes |
| gate5-anti | anti-causation | 3 | 0 | 0.0% | 0.0% | 0 | 9660.6 | 9912.1 | None/None (+3 missing) | bdecbea4 | yes |
| gate5-model-r2 | model | 4 | 2 | 50.0% | 50.0% | 0 | 115508.3 | 610469.3 | 84810/4187 | bdecbea4 | yes |
| gate5-anti-r2 | anti-causation | 3 | 0 | 0.0% | 0.0% | 0 | 436903.0 | 571074.3 | 45315/3523 | bdecbea4 | yes |

Failure layers: gate2: none:11; gate2-second: none:11; gate5-smoke: none:11; gate5-model: none:2, unknown:2; gate5-anti: reasoning:3; gate5-model-r2: none:2, reasoning:2; gate5-anti-r2: reasoning:2, unknown:1

## Per-case stability

| run | case | repeats | passed | layers | p50 ms | p95 ms |
|---|---|---|---|---|---|---|
| gate2 | D-APPROVE | 1 | 1 | none:1 | 1061.8 | 1061.8 |
| gate2 | D-BUDGET:llm_calls | 1 | 1 | none:1 | 3.5 | 3.5 |
| gate2 | D-BUDGET:steps | 1 | 1 | none:1 | 4.7 | 4.7 |
| gate2 | D-BUDGET:wall_clock | 1 | 1 | none:1 | 210.4 | 210.4 |
| gate2 | D-CANCEL | 1 | 1 | none:1 | 62.3 | 62.3 |
| gate2 | D-CIRCUIT | 1 | 1 | none:1 | 6.2 | 6.2 |
| gate2 | D-EV | 1 | 1 | none:1 | 5.5 | 5.5 |
| gate2 | D-FAIL | 1 | 1 | none:1 | 5.4 | 5.4 |
| gate2 | D-GAP | 1 | 1 | none:1 | 4.1 | 4.1 |
| gate2 | D-GROUND | 1 | 1 | none:1 | 4.5 | 4.5 |
| gate2 | D-SEL | 1 | 1 | none:1 | 1759.9 | 1759.9 |
| gate2-second | D-APPROVE | 1 | 1 | none:1 | 1027.6 | 1027.6 |
| gate2-second | D-BUDGET:llm_calls | 1 | 1 | none:1 | 3.8 | 3.8 |
| gate2-second | D-BUDGET:steps | 1 | 1 | none:1 | 4.6 | 4.6 |
| gate2-second | D-BUDGET:wall_clock | 1 | 1 | none:1 | 211.7 | 211.7 |
| gate2-second | D-CANCEL | 1 | 1 | none:1 | 64.8 | 64.8 |
| gate2-second | D-CIRCUIT | 1 | 1 | none:1 | 5.4 | 5.4 |
| gate2-second | D-EV | 1 | 1 | none:1 | 5.8 | 5.8 |
| gate2-second | D-FAIL | 1 | 1 | none:1 | 5.4 | 5.4 |
| gate2-second | D-GAP | 1 | 1 | none:1 | 4.3 | 4.3 |
| gate2-second | D-GROUND | 1 | 1 | none:1 | 4.6 | 4.6 |
| gate2-second | D-SEL | 1 | 1 | none:1 | 1694.8 | 1694.8 |
| gate5-smoke | D-APPROVE | 1 | 1 | none:1 | 1001.7 | 1001.7 |
| gate5-smoke | D-BUDGET:llm_calls | 1 | 1 | none:1 | 4.5 | 4.5 |
| gate5-smoke | D-BUDGET:steps | 1 | 1 | none:1 | 4.5 | 4.5 |
| gate5-smoke | D-BUDGET:wall_clock | 1 | 1 | none:1 | 213.7 | 213.7 |
| gate5-smoke | D-CANCEL | 1 | 1 | none:1 | 71.3 | 71.3 |
| gate5-smoke | D-CIRCUIT | 1 | 1 | none:1 | 6.6 | 6.6 |
| gate5-smoke | D-EV | 1 | 1 | none:1 | 5.8 | 5.8 |
| gate5-smoke | D-FAIL | 1 | 1 | none:1 | 7.0 | 7.0 |
| gate5-smoke | D-GAP | 1 | 1 | none:1 | 4.3 | 4.3 |
| gate5-smoke | D-GROUND | 1 | 1 | none:1 | 5.0 | 5.0 |
| gate5-smoke | D-SEL | 1 | 1 | none:1 | 1792.1 | 1792.1 |
| gate5-model | M-CANON | 2 | 0 | unknown:2 | 8724.1 | 8760.7 |
| gate5-model | M-SEL | 2 | 2 | none:2 | 112106.2 | 113382.9 |
| gate5-anti | V:deployment_without_incident | 1 | 0 | reasoning:1 | 9912.1 | 9912.1 |
| gate5-anti | V:incident_without_deployment | 1 | 0 | reasoning:1 | 9660.6 | 9660.6 |
| gate5-anti | V:no_anomaly | 1 | 0 | reasoning:1 | 9129.6 | 9129.6 |
| gate5-model-r2 | M-CANON | 2 | 0 | reasoning:2 | 443896.8 | 610469.3 |
| gate5-model-r2 | M-SEL | 2 | 2 | none:2 | 111387.8 | 115508.3 |
| gate5-anti-r2 | V:deployment_without_incident | 1 | 0 | reasoning:1 | 436903.0 | 436903.0 |
| gate5-anti-r2 | V:incident_without_deployment | 1 | 0 | unknown:1 | 254344.3 | 254344.3 |
| gate5-anti-r2 | V:no_anomaly | 1 | 0 | reasoning:1 | 571074.3 | 571074.3 |
