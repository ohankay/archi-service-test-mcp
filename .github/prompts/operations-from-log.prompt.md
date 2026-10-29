---
mode: agent
description: Define new operations from an app (or HAR) log
tools: ['archi-service-test/*']
---
Evaluate the log file `${input:log_file}`:

1. PREVIEW it with `import_log` (without apply). Do not try to read the log yourself; the server reads the file.
2. Summarize for me: which environment was found, which operations are new, which are already defined (any new field
   not in the definition), which calls were counted as noise or outside the systems.
3. For each new operation ask for a friendly name and which inputs matter; for date and number inputs ask whether the
   limits are known (min / max, a date range).
4. If I approve, save the drafts with `apply=true`, then apply my decisions with `save_definition`.
5. Make the first real call with `call_operation` and show the checks you would suggest from the response.
