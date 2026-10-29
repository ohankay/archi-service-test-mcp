---
mode: agent
description: Find out why a test failed
tools: ['archi-service-test/*']
---
The test `${input:test}` failed with customer `${input:customer}`. Find the reason:

1. Run it again with `run_test`; show the failed check, the expected and the actual value, and the `problem` line.
2. If needed, call the operation on its own with `call_operation` and look at the response and the `flow`.
3. Tell apart whether the problem is in the service (this is not the expected behaviour), in the test (a wrong check
   / the data changed) or in the environment (connection, session - use `check_connection`). If it is the test,
   suggest a fix, but never loosen a check just to make it pass.
