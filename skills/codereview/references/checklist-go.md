# Go Language Checklist

Language-specific footguns for Go code review. Auto-injected when the diff contains `.go` files.
Sourced from real code review discussions (Baz Awesome Reviewers).

## Concurrency

- [ ] **Missing defer on mutex unlock**: Is every `mu.Lock()` immediately followed by `defer mu.Unlock()`? A panic or early return between lock and unlock causes a deadlock. Bad: `mu.Lock(); doWork(); mu.Unlock()`. Good: `mu.Lock(); defer mu.Unlock(); doWork()`.
  <!-- vitess-prevent-concurrent-access-races.md, influxdb-lock-with-defer-unlock.md -->
- [ ] **Shared map/slice without synchronization**: Is a map or slice written by one goroutine and read/written by another without a mutex or channel? Maps are not safe for concurrent use. Bad: `go func() { m[k] = v }()`. Good: wrap with `sync.Mutex` or use `sync.Map`.
  <!-- waveterm-protect-shared-state.md, grafana-safe-concurrent-programming.md -->
- [ ] **Goroutine leak via context.Background()**: Does a spawned goroutine use `context.Background()` instead of inheriting the parent context? It will not be cancelled when the parent is done. Bad: `go doWork(context.Background())`. Good: `go doWork(ctx)`.
  <!-- grafana-safe-concurrent-programming.md, istio-prevent-race-conditions.md -->
- [ ] **time.Sleep ignoring cancellation**: Is `time.Sleep(d)` used in a goroutine instead of a select on `ctx.Done()`? The goroutine cannot be cancelled during sleep. Bad: `time.Sleep(5*time.Second)`. Good: `select { case <-time.After(5*time.Second): case <-ctx.Done(): return }`.
  <!-- istio-prevent-race-conditions.md -->
- [ ] **Returning internal map/slice without copy**: Does a method return an internal slice or map to callers? Other goroutines or callers can mutate the backing data. Good: return `slices.Clone(s.items)` or `maps.Clone(s.data)`.
  <!-- waveterm-protect-shared-state.md, vitess-prevent-concurrent-access-races.md -->
- [ ] **Lock ordering not documented**: When acquiring multiple locks, is the order consistent and documented? Inconsistent lock ordering causes deadlocks that only manifest under load.
  <!-- opentofu-safe-lock-patterns.md, terraform-guard-shared-state.md -->

## Nil & Panic Safety

- [ ] **Nil dereference after type assertion**: Does the code access fields on a value obtained from a type assertion without checking nil? A successful type assertion (`.(*T)`) can still yield a nil pointer. Bad: `if v, ok := x.(*Foo); ok { v.Bar() }` when `x` holds a typed nil. Good: add `v != nil` check.
  <!-- volcano-add-explicit-nil-checks.md -->
- [ ] **Missing nil check on pointer parameter**: Does a function dereference a pointer parameter without checking nil? Exported functions and interface implementations should guard against nil receivers and arguments.
  <!-- influxdb-prevent-nil-dereferences.md, grafana-explicit-null-validation.md -->
- [ ] **Panic in production error path**: Is `panic()` used for error handling instead of returning an error? Panics in library code or hot paths crash the entire process. Bad: `panic(err)`. Good: `return fmt.Errorf("operation failed: %w", err)`.
  <!-- prometheus-avoid-panics-gracefully.md -->
- [ ] **Nil/empty slice confusion in serialization**: Does code treat nil slices and empty slices (`[]T{}`) as equivalent? In JSON marshaling `nil` becomes `null` while `[]T{}` becomes `[]`, which can break API contracts.
  <!-- sonic-empty-vs-nil-distinction.md -->

## Error Handling

- [ ] **Bare error return without context**: Is `return err` used instead of `return fmt.Errorf("what failed: %w", err)`? Bare returns produce opaque error chains that are impossible to debug in production.
  <!-- argo-cd-wrap-errors-with-context.md, influxdb-wrap-errors-with-context.md -->
- [ ] **Unchecked error return**: Is an error return value assigned to `_` or simply ignored? Even "unlikely" errors (Close, Flush, regex compile) can indicate real failures. Bad: `f.Close()`. Good: `if err := f.Close(); err != nil { log.Warn(...) }`.
  <!-- fiber-check-all-error-returns.md, cli-handle-all-errors-explicitly.md -->
- [ ] **Resource leak on error path**: When an error occurs after opening a resource (file, connection, response body), is the resource closed? Use `defer` immediately after successful open, before the next error check.
  <!-- terraform-resource-cleanup-on-errors.md, grafana-close-resources-with-errors.md -->
- [ ] **Close error silenced on write path**: For writable resources (file, gzip writer, DB transaction), is the `Close()` error propagated? A failed Close on a writer can mean data was not flushed. Bad: `defer w.Close()`. Good: `defer func() { if cerr := w.Close(); err == nil { err = cerr } }()`.
  <!-- grafana-close-resources-with-errors.md -->

## Memory & Performance

- [ ] **Slice append aliasing (shared backing array)**: Is `append(existingSlice, elem)` used when `existingSlice` was derived from a sub-slice? If the original has spare capacity, the append overwrites the parent's data. Fix: use `slices.Clip(s)` before appending, or copy explicitly.
  <!-- opentofu-prevent-backing-array-surprises.md -->
- [ ] **Allocation in hot path**: Is a buffer, slice, or map allocated on every call to a frequently-invoked function? Pre-allocate with known capacity (`make([]T, 0, n)`) or reuse via a struct field / `sync.Pool`. Bad: `tokens := make([]T, len(input))` per call. Good: grow a reusable `s.buf` field.
  <!-- prometheus-minimize-memory-allocations.md, ollama-reuse-buffers-strategically.md -->
- [ ] **fmt.Sprintf in hot path**: Is `fmt.Sprintf` used for string building in a tight loop? `strings.Builder` is 4x faster with fewer allocations. Bad: `key = fmt.Sprintf("%s-%s-%d", a, b, c)`. Good: use `strings.Builder` with `WriteString`/`WriteString`.
  <!-- istio-avoid-expensive-operations.md -->

## Networking

- [ ] **HTTP call without context/timeout**: Is `http.Get()`, `http.Post()`, or `http.DefaultClient.Do()` called without a context or timeout? The call can hang forever. Bad: `http.Get(url)`. Good: `req, _ := http.NewRequestWithContext(ctx, "GET", url, nil); client.Do(req)`.
  <!-- waveterm-use-network-timeouts.md, temporal-context-aware-network-calls.md -->
- [ ] **Magic numbers in network code**: Are raw integers used for buffer sizes, port numbers, or protocol constants instead of named constants from `syscall`/`net` packages? Bad: `if status != 0x01`. Good: `if status != windows.IfOperStatusUp`.
  <!-- go-use-proper-network-constants.md -->

## Security

- [ ] **Unsanitized path in file operation**: Is user input used to construct a file path without validation or `filepath.Clean`/`securejoin`? This enables path traversal (`../../../etc/passwd`). Always validate against an allowlist or use `securejoin.SecureJoin`.
  <!-- argo-cd-validate-untrusted-inputs.md -->
- [ ] **Secrets in error messages or logs**: Does an error message or log line include raw user input, environment variables, or credentials? Log the event, not the value. Bad: `fmt.Errorf("bad env: %s", line)`. Good: `fmt.Errorf("bad env at line %d", lineNum)`.
  <!-- kubernetes-prevent-information-disclosure.md -->
- [ ] **SQL built with string concatenation**: Is a SQL query built using `fmt.Sprintf` or `+` with user-supplied values instead of parameterized queries / bind variables? This is a SQL injection vector.
  <!-- vitess-use-parameterized-queries.md -->

## Observability

- [ ] **Trace span not closed in all paths**: Is a span created with `tracer.Start()` but not ended on every return path? Unclosed spans leak memory and distort trace data. Always use `defer span.End()` immediately after creation. In loops, extract the body to a helper function so defer works per iteration.
  <!-- opentofu-proper-span-lifecycle.md -->
- [ ] **High-cardinality metric label**: Is a metric tagged with a label that has unbounded cardinality (hostname, user ID, request path)? This explodes memory in Prometheus. Use counters without the label, or bucket into a fixed set of values.
  <!-- temporal-optimize-metrics-label-cardinality.md, vitess-metric-design-best-practices.md -->

## Database & Migrations

- [ ] **Non-idempotent migration**: Does a migration use raw DDL without `IF NOT EXISTS` / `IF EXISTS` guards? Re-running the migration will fail. Always use idempotent operations or structured migration APIs.
  <!-- signoz-ensure-migration-idempotency.md -->
