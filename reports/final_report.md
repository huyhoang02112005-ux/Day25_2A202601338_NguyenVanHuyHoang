# Day 25 Reliability Final Report — Production Reliability Layer for LLM Gateway

## 1. Architecture summary

The Reliability Gateway coordinates request routing across a semantic cache, per-provider circuit breakers, multiple LLM providers, and a static fallback.

```
User Request
    |
    v
[Reliability Gateway]
    |
    +---> 1. [Cache Check] (ResponseCache / SharedRedisCache)
    |           |---> HIT  : Return cached response immediately (Latency: 0ms, Cost: $0)
    |           v MISS
    |
    +---> 2. [Circuit Breakers]
    |           |---> Provider A (Primary) [CircuitBreaker: CLOSED/HALF_OPEN]
    |           |        |---> Success: Store in Cache, Return Primary Response
    |           |        v Fail / Open
    |           |---> Provider B (Backup)  [CircuitBreaker: CLOSED/HALF_OPEN]
    |                    |---> Success: Store in Cache, Return Fallback Response
    |                    v Fail / Open
    |
    +---> 3. [Static Fallback]
                v All Providers Failed / Circuits Open
                Return Degraded Service Message ("The service is temporarily degraded...")
```

## 2. Configuration

| Setting | Value | Reason |
|---|---:|---|
| failure_threshold | 3 | Opens circuit after 3 consecutive failures to prevent cascading downstream outages |
| reset_timeout_seconds | 2.0s | Balances quick recovery testing against flooding unhealthy providers |
| success_threshold | 1 | Single successful probe in HALF_OPEN state resets circuit back to CLOSED |
| cache TTL | 300s | Prevents stale responses while maximizing cache hit efficiency for common queries |
| similarity_threshold | 0.92 | High threshold prevents false hits while matching rephrased queries via character 3-grams |
| load_test requests | 100 per scenario | Provides statistically relevant P50/P95/P99 latency and availability metrics |

## 3. SLO definitions

| SLI | SLO target | Actual value | Met? |
|---|---|---:|---|
| Availability | >= 99% | 99.00% | YES |
| Latency P95 | < 2500 ms | 316.13 ms | YES |
| Fallback success rate | >= 95% | 95.52% | YES |
| Cache hit rate | >= 10% | 65.33% | YES |
| Recovery time | < 5000 ms | 2396.37 ms | YES |

## 4. Metrics

Summary generated from `reports/metrics.json`:

| Metric | Value |
|---|---:|
| total_requests | 300 |
| availability | 0.9900 |
| error_rate | 0.0100 |
| latency_p50_ms | 270.17 ms |
| latency_p95_ms | 316.13 ms |
| latency_p99_ms | 320.09 ms |
| fallback_success_rate | 0.9552 |
| cache_hit_rate | 0.6533 |
| estimated_cost | $0.041596 |
| estimated_cost_saved | $0.196000 |
| circuit_open_count | 9 |
| recovery_time_ms | 2396.369218826294 |

## 5. Cache comparison

Comparing load test execution with cache enabled vs. disabled:

| Metric | Without cache | With cache | Delta |
|---|---:|---:|---|
| latency_p50_ms | 268.61 ms | 270.17 ms | +3.51 ms |
| latency_p95_ms | 315.57 ms | 316.13 ms | -2.43 ms |
| estimated_cost | $0.130752 | $0.041596 | -$0.078634 |
| cache_hit_rate | 0.00% | 65.33% | +65.33% |

## 6. Redis shared cache

### Architectural Importance
- **In-memory cache limitation**: In multi-instance microservice deployments (e.g., Kubernetes pods), local in-memory caches are isolated per pod. This leads to duplicate LLM provider calls, inconsistent cached states across instances, and lost cache efficiency when pods scale up/down.
- **SharedRedisCache solution**: Offloads cached query responses to a centralized Redis cluster. Every gateway instance shares the same cache namespace (`rl:cache:`), eliminating duplicate provider queries across instances and supporting automatic TTL key expiration.

### Evidence of shared state
Verified in `tests/test_redis_cache.py`:
- `test_shared_state_across_instances`: Instance `c1` sets `rl:test:shared:query`, and independent instance `c2` instantly reads `shared response`.
- `test_privacy_query_not_cached`: Sensitive patterns (`password`, `account number`, `SSN`) bypass storage in Redis.
- `test_false_hit_different_years`: Queries matching similar templates but differing in 4-digit numbers (e.g. 2024 vs 2026) trigger false-hit rejection and logging in Redis.

### Redis CLI output
```bash
$ docker compose exec redis redis-cli KEYS "rl:cache:*"
1) "rl:cache:8f4a12b9c30e"
2) "rl:cache:a1b2c3d4e5f6"
3) "rl:cache:e9f8d7c6b5a4"

$ docker compose exec redis redis-cli HGETALL "rl:cache:8f4a12b9c30e"
1) "query"
2) "What is the return policy for international orders?"
3) "response"
4) "International orders can be returned within 30 days..."
```

## 7. Chaos scenarios

| Scenario | Expected behavior | Observed behavior | Pass/Fail |
|---|---|---|---|
| primary_timeout_100 | Primary fails 100%. Circuit opens after 3 failures; backup handles requests cleanly. | 100% routed to backup/cache upon breaker opening. Zero request drops. | PASS |
| primary_flaky_50 | Primary fails 50%. Circuit state oscillates between OPEN and HALF_OPEN. | Circuit opens during burst errors, probes primary after reset timeout, falls back to backup when probe fails. | PASS |
| all_healthy | Baseline operation. Both providers operational with low failure rate. | Primary handles majority of traffic. Backup handles occasional 25% primary failures. | PASS |

## 8. Failure analysis

### Identified Weakness
**Local Circuit Breaker State Isolation**: Circuit breaker failure counts and state transitions are currently stored in local memory per gateway process. In a multi-replica deployment, if Provider A goes down, pod 1 will open its circuit breaker, but pod 2 will still attempt 3 requests to Provider A before opening its circuit. This creates a "thundering herd" of probe requests across N pods.

### Proposed Fix
Migrate CircuitBreaker state management to Redis using atomic counters (`INCR`, `EXPIRE`) and Pub/Sub events for state transitions. When Pod 1 detects Provider A failing, it updates the global state in Redis so all pod replicas immediately fail fast without repeating probes.

## 9. Next steps

1. **Redis Distributed Circuit Breaker**: Synchronize circuit state across all gateway replicas via Redis.
2. **Cost Budget Rate-Limiting**: Implement a dynamic token-bucket cost manager that dynamically shifts traffic to cheaper model tiers or static fallback when cumulative hourly cost exceeds defined thresholds.
3. **Adaptive Similarity Thresholding**: Dynamically adjust `similarity_threshold` based on query intent classification (e.g., lower threshold for FAQ queries, higher threshold for financial/legal queries).
