Review this diff for security risks.

Focus areas:
1. Authentication and authorization boundary violations.
2. Missing input validation and injection vectors (SQL, XSS, command, template).
3. Secrets exposure or unsafe credential handling.
4. SSRF, path traversal, unsafe deserialization, and trust-boundary mistakes.
5. Dependency vulnerabilities introduced by new imports.

Investigation approach:
- Use Grep to trace data flow from user input to sensitive operations.
- Use Read to check if validation/sanitization exists upstream of the changed code.
- Check if new endpoints have proper auth middleware.
- Look for hardcoded secrets, API keys, or tokens in the diff.

For each finding include exploit preconditions and remediation.
Return ALL findings. Use the JSON schema from the global contract.
