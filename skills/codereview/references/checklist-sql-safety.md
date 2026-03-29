# SQL Safety Checklist

Check each item. If the answer is "yes" for any, report a finding with evidence.

- [ ] Does any SQL query use string concatenation or f-strings instead of parameterized queries?
- [ ] Does any ORM query use `.raw()`, `.execute()`, or `.extra()` with user-controlled input?
- [ ] Is there a query inside a loop that could be an N+1 pattern?
- [ ] Are database transactions missing where multiple related writes should be atomic?
- [ ] Is `SELECT *` used where only specific columns are needed?
- [ ] Are there queries without `LIMIT` that could return unbounded result sets?
- [ ] Is user input used in `ORDER BY`, `GROUP BY`, or table/column names without a whitelist?
- [ ] Are database migrations missing for schema changes introduced in this diff?
- [ ] Does any query build dynamic SQL from configuration or environment variables?
- [ ] Are connection pools or database connections opened but never closed or released?
- [ ] Is there a `DELETE` or `UPDATE` statement missing a `WHERE` clause?
- [ ] Are SQL error messages exposed to users, potentially leaking schema details?
- [ ] Are there bulk insert/update operations that could exceed database limits or timeouts?
- [ ] Is sensitive data (passwords, tokens, PII) stored in plaintext columns?
- [ ] Are database queries missing indexes on columns used in `WHERE`, `JOIN`, or `ORDER BY` clauses?
