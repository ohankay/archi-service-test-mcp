---
mode: agent
description: Write and run a step test for a described flow
tools: ['archi-service-test/*']
---
Prepare a test for this flow: `${input:flow}`

1. Find the operations to use with `list_tests`; say if one is missing.
2. Write a step test (`steps`): when a step's output is the next step's input, pass it with `extract`; put at least
   one business rule check on every step; write rules for data that changes. Expectations that cannot be written as
   checks become `ai_review`.
3. Run it with `run_test(content=..., customer=${input:customer})` and explain the result step by step.
4. If I approve, save it with `save_definition`.
