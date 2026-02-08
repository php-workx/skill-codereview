Review this diff for test adequacy.

Focus areas:
1. Missing tests for changed control flow and new branches.
2. Missing tests for failure handling and error paths.
3. Missing tests for integration boundaries and contracts.
4. Existing tests that now test stale behavior (tests that should be updated).
5. Mock-heavy tests that don't actually verify real behavior.

Investigation approach:
- Use Glob to find test files related to the changed code (e.g., `**/test_*.py`, `**/*.test.ts`).
- Use Read to examine existing tests — check if they cover the changed logic.
- Use Grep to find assertion patterns and verify they test real behavior, not just mocks.
- For each new function/method, check if at least one test exercises it.

Return ALL test gaps found. For each, describe the test scenario and which file/line it relates to.
Use the JSON schema from the global contract.
