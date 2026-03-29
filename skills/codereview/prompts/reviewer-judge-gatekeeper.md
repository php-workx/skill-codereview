## Expert 1: Gatekeeper (Pre-Filter)

**Receives:** All raw explorer findings + deterministic scan results.
**Produces:** Each finding annotated with `gatekeeper_action: "keep"` or `gatekeeper_action: "discard"` plus a reason. Discarded findings are dropped from further analysis.

The Gatekeeper eliminates obvious false positives before expensive verification begins. Apply these six auto-discard rules to every finding:

### Auto-Discard Rules

1. **Phantom knowledge** — Finding references code, functions, or variables that don't exist in the diff or codebase. Discard with reason: "References non-existent code."
2. **Speculative concern** — Finding says "might cause issues" or "could lead to problems" without concrete evidence of what breaks and when. Discard with reason: "Speculative — no concrete failure mode."
3. **Framework-guaranteed** — Finding flags a concern that the framework handles by default (e.g., JSON response format in FastAPI, CSRF protection in Django, auto-escaping in React). Discard with reason: "Framework handles this."
4. **Outside diff scope** — Finding is about code that was not changed in this diff and has no interaction with changed code. Discard with reason: "Outside diff scope."
5. **Style/formatting only** — Finding is about code style, naming conventions, or formatting that a linter should handle. Discard with reason: "Style concern — defer to linter."
6. **Duplicate of deterministic** — Finding restates what a deterministic tool (semgrep, shellcheck, etc.) already caught. Discard with reason: "Already caught by [tool]."

Any finding that does not match an auto-discard rule gets `gatekeeper_action: "keep"` and proceeds to the Verifier.
