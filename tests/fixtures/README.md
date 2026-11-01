# Transcript structure replay fixture

`agy-transcript-structure-replay.jsonl` is a structural projection of 65 USER_INPUT turns from six recent local Gemini transcripts. Prompt, answer, command text, filesystem paths, session IDs, and original task IDs were removed. It keeps event type, source, status, sequence order, tool names, re-keyed task references, and coarse task-state signals needed to replay completion decisions.

`legacy_unfinished` records the prior completion predicate; `expected_unfinished` records the reviewed result from the transcript task-state signals. The replay test asserts both the old 13 false unfinished results and the corrected classification.
