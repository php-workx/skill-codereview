# Python Language Checklist

Language-specific footguns for Python code review. Auto-injected when the diff contains `.py` files.
Sourced from real code review discussions (Baz Awesome Reviewers).

## Error Handling

- [ ] **Silent exception swallowing**: Does any `except` block use bare `except:` or `except Exception` with only `pass`, `return None`, or logging but no re-raise? Silent failures mask bugs downstream. Bad: `except Exception: return None`. Good: `except SpecificError as e: logger.error(...); raise`.
- [ ] **Overly broad try blocks**: Does a `try` block wrap more code than the single operation that can fail? This catches unrelated exceptions under the wrong handler. Move non-throwing code outside the `try`.
- [ ] **Missing exception chaining**: When catching and re-raising as a different type, is `from e` used? Without it, the original traceback is lost. Bad: `raise ValueError(msg)`. Good: `raise ValueError(msg) from e`.
- [ ] **Returning None for errors**: Does a function return `None` to signal failure instead of raising an exception? This forces every caller to check for `None` and failures propagate silently when they forget.
- [ ] **Exceptions as assertions**: Is `assert` used for runtime validation of user input or external data? Assertions are stripped when running with `python -O`. Use `if not x: raise ValueError(...)` instead.

## None/Null Safety

- [ ] **Mutable default arguments**: Does any function use a mutable default like `def f(items=[])` or `def f(config={})`? The default is shared across all calls. Use `None` and assign inside the body: `items = items or []`.
- [ ] **Missing Optional annotation**: Is a parameter typed as `str` but defaulted to `None`? This should be `Optional[str] = None` to match the actual contract.
- [ ] **Unsafe dict access**: Is `d["key"]` used on a dict from external input (API response, config, JSON) without checking existence? Use `d.get("key", default)` or chain with `.get("nested", {}).get("key")`.
- [ ] **Falsy-value confusion with `or`**: Is `x or default` used where `x` could legitimately be `0`, `""`, or `False`? These falsy values will be replaced by the default. Use `x if x is not None else default` instead.
- [ ] **Returning None where a collection is expected**: Does a function return `None` when it has no results instead of an empty list/dict/set? Callers that iterate or call `.items()` on the result will crash.

## Concurrency

- [ ] **Wrong lock type for context**: Is `threading.Lock` used in async code or `asyncio.Lock` used across threads? Each only protects within its own concurrency model. Async coroutines in a single thread usually need no lock at all.
- [ ] **Missing timeout on blocking calls**: Do `thread.join()`, `future.result()`, `queue.get()`, or `requests.get()` lack a `timeout` parameter? Without it, the call can block indefinitely and hang the process.
- [ ] **Manual lock acquire/release without context manager**: Is `lock.acquire()` / `lock.release()` used instead of `with lock:`? If an exception occurs between acquire and release, the lock is never freed.
- [ ] **Fire-and-forget tasks without tracking**: Are `asyncio.create_task()` or `executor.submit()` called without storing the returned future? Exceptions in the task are silently lost and the task may be garbage-collected.
- [ ] **Check-then-act race**: Is there a pattern like `if not exists: create()` on shared state without a lock? Another thread can create between the check and the act.

## Performance

- [ ] **Python loop over tensors/arrays**: Is a `for` loop or list comprehension used to process NumPy arrays or PyTorch tensors element-by-element? Vectorized operations (`np.where`, `torch.gather`, broadcasting) are orders of magnitude faster.
- [ ] **Repeated expensive call in a loop**: Is an expensive function (DB query, API call, regex compile, file read) called inside a loop when the result could be computed once before the loop?
- [ ] **List where set is needed for membership tests**: Is `item in large_list` used for membership checks? Use a `set` for O(1) lookup. Bad: `if x in [a, b, c, ...]` on a large collection. Good: `if x in {a, b, c, ...}`.
- [ ] **Building a large list in memory when streaming would work**: Is `results.extend(all_items)` accumulating unbounded data? Process items in a streaming/generator fashion or write to disk incrementally.

## Database / ORM

- [ ] **N+1 query pattern**: Is a related object accessed inside a loop over a queryset without `select_related()` or `prefetch_related()`? Each iteration fires a separate SQL query.
- [ ] **Non-deterministic query ordering**: Does a query with `LIMIT` or pagination lack an `ORDER BY` on a unique column? Results may shift between pages. Add `.order_by("id")` as a tiebreaker.
- [ ] **Unsafe migration sequence**: Does a migration add a NOT NULL column without a default to a table with existing data? This will fail. Add the column as nullable first, backfill, then add the constraint in a separate migration.

## Security

- [ ] **Secrets in log output**: Are API keys, tokens, passwords, or URLs containing credentials passed to `logger.info()`, `print()`, or f-strings that appear in logs? Mask or redact before logging.
- [ ] **Path traversal from user input**: Is user-supplied input used in `open()`, `Path()`, or file operations without resolving and checking it stays within an allowed directory? Use `Path.resolve()` and verify with `is_relative_to()`.

---

## Provenance

Each item above traces to one or more Baz Awesome Reviewers rules (filename in `_reviewers/`):

| Checklist item | Source rule(s) |
|---|---|
| Silent exception swallowing | `dspy-avoid-silent-failures.md`, `vllm-catch-specific-exception-types.md`, `prowler-specific-exception-handling.md` |
| Overly broad try blocks | `core-minimize-try-block-scope.md` |
| Missing exception chaining | `parlant-preserve-exception-context.md`, `serena-exception-chaining-practices.md`, `django-preserve-error-handling-context.md` |
| Returning None for errors | `dify-prefer-exceptions-over-silent-failures.md`, `unstructured-raise-exceptions-properly.md` |
| Exceptions as assertions | `heretic-surface-errors-clearly.md`, `parlant-preserve-exception-context.md` |
| Mutable default arguments | `dify-safe-null-handling.md` |
| Missing Optional annotation | `unstructured-explicit-none-handling.md`, `sglang-use-optional-types-safely.md` |
| Unsafe dict access | `checkov-safe-dictionary-navigation.md`, `litellm-safe-access-patterns.md` |
| Falsy-value confusion with `or` | `litellm-safe-access-patterns.md` |
| Returning None where collection expected | `opentelemetry-python-return-collections-not-none.md` |
| Wrong lock type for context | `sdk-python-choose-appropriate-synchronization.md` |
| Missing timeout on blocking calls | `sentry-thread-management-best-practices.md`, `poetry-configure-http-requests-properly.md` |
| Manual lock without context manager | `lmcache-ensure-operation-completion-safety.md`, `openpilot-use-context-managers-concurrency.md` |
| Fire-and-forget tasks | `litellm-background-task-coordination.md` |
| Check-then-act race | `sglang-prevent-race-conditions.md` |
| Python loop over tensors/arrays | `vllm-vectorize-over-python-loops.md` |
| Repeated expensive call in loop | `sglang-eliminate-redundant-operations.md`, `pydantic-cache-expensive-computations.md` |
| List where set needed | `checkov-choose-optimal-algorithms.md` |
| Building large list in memory | `prowler-memory-usage-optimization.md` |
| N+1 query pattern | `sentry-prevent-n1-database-queries.md`, `posthog-optimize-orm-queries.md` |
| Non-deterministic query ordering | `airflow-ensure-deterministic-queries.md` |
| Unsafe migration sequence | `prowler-ensure-migration-compatibility.md`, `posthog-split-complex-migrations-incrementally.md`, `airflow-safe-constraint-modification-sequence.md` |
| Secrets in log output | `checkov-prevent-sensitive-data-exposure.md`, `sentry-secure-sensitive-data.md` |
| Path traversal from user input | `airflow-validate-user-controlled-paths.md`, `langflow-prevent-code-injection-vulnerabilities.md` |
