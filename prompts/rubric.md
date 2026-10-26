# Grading Rubric

> Works for any subject. Version: `generic-1`.

Criteria are independent. Missing a template does not lower correctness; incomplete
justification for a correct answer is a structure defect, not a correctness defect.

| Criterion | Scale | Meaning |
|---|---|---|
| **Legibility** (`legibility`) | 0–2 | 2 — readable on the first pass; 1 — occasionally requires rereading; 0 — contains undecipherable fragments |
| **Template** (`template`) | 0–2 | A clear layout: what is given, what is asked, numbered steps, a final answer (for essays: thesis, body, conclusion). 2 — all; 1 — partial; 0 — no layout |
| **Structure** (`structure`) | 0–3 | Each step refers to a given fact or the previous step. 3 — complete chain; 2 — one unjustified transition; 1 — several; 0 — no chain |
| **Correctness** (`correctness`) | 0–3 or NULL | 3 — answer is correct and the solution reaches it; 2 — correct, but a **case** is omitted in the case analysis; 1 — reasoning is correct, answer is incorrect; 0 — incorrect. NULL when `needs_human` |
| **Total** | 0–10 | sum; NULL if `correctness` = NULL |

## Correctness: what does NOT lower the grade

- missing the layout (given / steps / answer) — this is `template`;
- missing justification for a correct transition — this is `structure` and `unjustified_leap`;
- a nonstandard but logically correct approach (e.g. colouring instead of parity, or a different valid argument) — not an error.

## Correctness: what lowers the grade

- incorrect answer (0, or 1 if the reasoning was correct but the answer was spoiled by an arithmetic error);
- omitted case in a problem requiring case analysis (2);
- a conclusion that does not follow from the statements given, even if the answer was guessed correctly (0).

## Separate indicator

`unjustified_leap: bool` — whether the submission contains a statement that follows neither from the given information
nor from the previous step. The main reasoning habit being trained; tracked separately from scores
in the dashboard.

## Aggregation across problems

`correctness` for the submission = **minimum** across problems. If even one problem yields NULL —
the entire submission is NULL and `needs_human=1`.
