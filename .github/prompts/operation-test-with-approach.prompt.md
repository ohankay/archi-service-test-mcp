---
mode: agent
description: Test an operation with a chosen test approach and report the result
tools: ['archi-service-test/*']
---
Test the operation `${input:operation}` with the `${input:approach}` approach, with customer `${input:customer}` in
the `${input:environment}` environment:

1. Check with `list_tests` that the operation and the approach are defined; if not, say so and suggest how to define
   them.
2. Make the draft table with `generate_tests`; if there are `notes` (missing limits), ask me.
3. Run it with `run_test(content=..., customer=..., environment=...)`.
4. Explain the result plainly: what passed, what failed (expected and actual), the observations (ask "is this
   right?" for each).
5. If I approve, save the test with `save_definition`; put my decisions on the observations into `expected`. When
   done, `end_sessions`.
