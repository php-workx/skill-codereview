# References

The review prompt files are co-located with the skill:

```
prompts/
├── reviewer-global-contract.md            # Shared rules + JSON output schema
├── reviewer-correctness-pass.md           # Functional correctness pass
├── reviewer-security-pass.md              # Security risk pass
├── reviewer-reliability-performance-pass.md  # Reliability/performance pass
└── reviewer-test-adequacy-pass.md         # Test adequacy gap analysis
```

These are read at runtime by the skill during Step 4 (explorer sub-agents and review judge).

Also in this directory:

- `design.md` — Architecture diagram, design rationale table, and future v2 plans (not needed at runtime)
