# References

The review prompt files used by this skill live in the workspace root:

```
prompts/
├── reviewer-global-contract.md            # Shared rules + JSON output schema
├── reviewer-correctness-pass.md           # Functional correctness pass
├── reviewer-security-pass.md              # Security risk pass
├── reviewer-reliability-performance-pass.md  # Reliability/performance pass
└── reviewer-test-adequacy-pass.md         # Test adequacy gap analysis
```

These are read at runtime by the skill during Step 4 (explorer sub-agents and review judge).
