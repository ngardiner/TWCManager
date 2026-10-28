---
pr: 705
authors: MikeBishop
---

Drop TeslaBLE's internal retries and circuit breaker (`maxRetries`, `retryDelay` and `circuitBreaker*` are ignored); the reconciler retries instead.
