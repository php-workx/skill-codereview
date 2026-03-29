# Concurrency Safety Checklist

Check each item. If the answer is "yes" for any, report a finding with evidence.

- [ ] Is shared mutable state accessed from multiple threads/goroutines without a lock or atomic operation?
- [ ] Are locks acquired in inconsistent order across different code paths, risking deadlock?
- [ ] Is there a time-of-check-to-time-of-use (TOCTOU) race condition?
- [ ] Are goroutines, threads, or async tasks spawned without a mechanism to wait for completion or cancellation?
- [ ] Is there a channel or queue that can grow unbounded, risking memory exhaustion?
- [ ] Are errors from spawned tasks or goroutines silently ignored?
- [ ] Is `async def` mixed with blocking I/O calls that could stall the event loop?
- [ ] Are thread-safe collections used where needed (e.g., `ConcurrentHashMap` vs `HashMap`)?
- [ ] Is there a lock held across an `await` point, risking deadlock in async code?
- [ ] Are atomic operations used correctly (e.g., proper memory ordering in Rust/C++)?
- [ ] Is there shared state modified in a `Promise.all` or parallel map without synchronization?
- [ ] Are resources (connections, file handles) shared across threads without being thread-safe?
- [ ] Is there a spin loop or busy-wait that should use a condition variable or channel?
- [ ] Are cancellation tokens or context deadlines propagated to child tasks?
- [ ] Is there a singleton or global variable initialized lazily without thread-safe initialization?
