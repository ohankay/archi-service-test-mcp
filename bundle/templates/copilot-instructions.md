## Service tests

- Do NOT use a terminal, PowerShell, curl or `Invoke-WebRequest` for service calls; use the `archi-service-test` MCP tools.
- Start with `list_tests`: the defined systems, operations, tests and test approaches. Use only the names from there;
  when something not defined is asked for, say so and suggest `import_log` (for an operation) or `generate_tests`
  (for a test).
- Running a test: `run_test(test, customer, environment)`. The server does login, OTP and the session.
- "Scan X with <approach>": `generate_tests(approach, operation)` -> show the draft -> `run_test(content=...)` ->
  `save_definition` if the user approves.
- Do not decide `observation` results: show the behaviour to the user and ask whether it is right. Answers to
  `ai_review` questions are your opinion, not a pass/fail decision.
- Check lines: `success`, `count X >= 1`, `X is not empty`, `X matches ^TR`, `for each LIST: A <= B`,
  `X between a and b`. For data that changes (balance, rate, date) write a rule, not a fixed value.
- Report `result` and `problem` first; when something fails show the `flow` line, the error `phase`, the cause `chain`
  and `likely_causes`. Connection problems: `check_connection`.
- Do not guess hidden values («secret: name»). When done, `end_sessions`.
