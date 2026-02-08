Review this diff for reliability and performance risks.

Focus areas:
1. Timeout, retry, idempotency, and fallback behavior.
2. Blocking I/O on hot paths and concurrency hazards.
3. N+1 patterns, query inefficiencies, and algorithmic complexity regressions.
4. Memory growth, resource leaks, and unnecessary allocations.
5. Missing circuit breakers or graceful degradation for external dependencies.

Investigation approach:
- Use Grep to find how changed functions are called in hot paths (request handlers, loops).
- Use Read to check if database queries are inside loops (N+1 pattern).
- If complexity scores are provided, flag functions with complexity C or worse.
- Look for unbounded collections, missing pagination, or missing timeouts.

Quantify likely impact where possible (e.g., "O(n^2) with n=users").
Return ALL findings. Use the JSON schema from the global contract.
