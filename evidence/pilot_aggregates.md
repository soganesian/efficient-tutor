# Pilot deployment — aggregate numbers (read-only query, 2026-10-26)

One family, one student (4th grade), one math tutor, 2 lessons/week.
Bot live since 2026-08-20; setup period archived; counted lessons from 2026-09-08.

| Metric | Value |
|---|---|
| Lessons on schedule (08.09–30.10) | 18 (13 completed and reported, 2 cancelled by tutor, 1 closed, rest upcoming) |
| Solution submissions | 24 (incl. 3 retakes) |
| First attempts submitted before the deadline | 15 of 18 (83%) |
| Grades produced | 21 (20 by AI, 1 without AI) |
| Grades flagged for human review (needs_human) | 20 of 21 |
| Works with an unjustified leap in reasoning | 15 of 21 |
| Hints used | 3 |
| Parent ✓/✗ marks given | 0 |
| Alerts the parent would otherwise not have had | 12 "tutor hasn't sent homework", 6 "homework missing", 4 "grading failed" |
| Reminders sent automatically | 17 to the student (24 h before), 6 to the tutor |
| LLM calls | 327, 0 failed after retries (Aug 24 – Oct 26) |
| Tokens | 777,702 input, 603,783 output |
| Tokens per graded homework (all calls, real handwritten photos, ~8 tasks) | 27.8k input, 25.0k output (n=15) |
| Avg grading call | 2,306 in / 2,075 out tokens, 14.2 s |
