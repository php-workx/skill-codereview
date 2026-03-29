## Expert 2: Verifier (Evidence Check)

**Receives:** All findings that survived the Gatekeeper (those with `gatekeeper_action: "keep"`).
**Produces:** Each finding annotated with a verification status: `verified`, `unverified`, or `disproven`. Disproven findings are dropped.

For EACH finding, use Grep, Read, and Glob tools to investigate — do not validate findings from memory alone.

### 2a. Existence Check

Verify the cited code actually exists at the stated file and line:
- Read the file at the stated line number.
- Confirm the code snippet in the `evidence` field matches what is actually there.
- If the line number is wrong but the issue exists nearby (within 5 lines), correct the line number and keep the finding.
- If the code does not exist or has been misread, mark the finding as `verification: "disproven"` and drop it.

### 2b. Contradiction Check

Actively search for evidence that **disproves** the finding. This is the most important step. For each finding type, look for the corresponding defense:

| Finding claims... | Search for... |
|-------------------|---------------|
| Missing null check | Null guard in the same function, in callers, in middleware, or guaranteed by type system |
| Missing error handling | Enclosing try/catch, error handler middleware, or framework-provided error boundary |
| Race condition | Mutex/lock protecting the access, single-threaded execution context, or immutable data |
| N+1 query | ORM eager loading, batch fetching, `select_related`/`prefetch_related`/`includes`/`preload` |
| SQL injection | Parameterized queries (`?`, `$1`, `:param`), ORM query builder, prepared statements |
| Missing input validation | Upstream validation in middleware, framework validation decorators, or type-safe deserialization |
| Missing auth | Auth middleware applied at router level, decorator on handler, or framework-level enforcement |
| Missing timeout | Context with deadline, HTTP client config, connection pool settings |
| Resource leak | `defer close()`, `try-with-resources`, `using` block, `finally` clause, context manager |

If you find a valid defense, mark the finding as `verification: "disproven"` and drop it, noting why in your working notes. If the defense is partial (covers some but not all paths), mark as `verification: "verified"` but note the partial defense — severity will be adjusted by the Calibrator.

### 2c. Verification Annotation

For every finding you keep, produce a verification status:

- **`verified`** — Code exists at the stated location, evidence references real lines, and no contradiction found. The finding is confirmed.
- **`unverified`** — Could not confirm the finding with Read/Grep (e.g., file exists but the specific pattern is ambiguous, or evidence is plausible but not conclusively verified). Downgrade confidence by 0.15. Note: the Calibrator will apply this adjustment.
- **`disproven`** — Evidence contradicts the finding (a valid defense exists, or the code doesn't exist as described). Drop the finding.
