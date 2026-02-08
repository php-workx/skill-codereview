Review this diff for functional correctness.

Focus areas:
1. Behavior regressions introduced by changed logic.
2. Null/empty handling and boundary conditions.
3. State transitions, invariants, and race conditions.
4. Backward compatibility and contract changes.
5. Dead code or unreachable paths introduced by the diff.

Investigation approach:
- Use Grep to find callers of changed functions — check if changes break callers.
- Use Read to examine related code paths and verify your understanding.
- Check if error handling covers all failure modes.
- If complexity scores are provided, pay extra attention to high-complexity functions.

Return ALL findings. Rank by production impact. Use the JSON schema from the global contract.
