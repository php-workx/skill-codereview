# LLM Trust Boundary Checklist

Check each item. If the answer is "yes" for any, report a finding with evidence.

- [ ] Is LLM output used in SQL queries, shell commands, or code evaluation without sanitization?
- [ ] Are API keys or secrets hardcoded rather than loaded from environment variables or a secrets manager?
- [ ] Is user input passed directly into prompts without escaping or input validation?
- [ ] Are LLM responses trusted for authorization decisions or access control?
- [ ] Is there missing rate limiting or cost controls on LLM API calls?
- [ ] Are LLM API errors silently swallowed instead of handled with retries or fallbacks?
- [ ] Is sensitive data (PII, credentials, internal documents) included in prompts sent to external APIs?
- [ ] Are LLM responses rendered as HTML or markdown without sanitizing for XSS?
- [ ] Is there missing validation of LLM output structure before parsing (e.g., assuming valid JSON)?
- [ ] Are token counts or prompt sizes unbounded, risking API failures or excessive costs?
- [ ] Is there a missing timeout on LLM API calls that could block indefinitely?
- [ ] Are model responses cached without considering staleness or prompt-sensitivity?
