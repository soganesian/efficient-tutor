# P1 — Independent solution (`solver`)

**Critical: the model does NOT receive the student's work.** It is run
`grading.independent_passes` times (3 by default) at temperature 1.

```
Solve the task the way a strong student at the level of this assignment would.
The subject can be anything: math, physics, languages, history, science.

Requirements:
- Solve it yourself, completely, through to the final answer.
- If the problem involves impossibility or case analysis — explicitly list all cases.
- If the problem statement is ambiguous or incomplete — don't make assumptions; note this.
- In `answer_key` give the final answer in its shortest canonical form, lowercase, no
  explanation: a number with units ("42 cm"), "yes" / "no", a single word or a short list.
  For open-ended tasks (essays, translations, free-form writing) put "open".
```

**Aggregating runs (revision 2026-08-18, C2).** The reference solution is accepted by majority:
an answer that matches in at least 2 out of 3 runs is considered the reference solution. Full spread
(all three answers are different) → `correctness` for this problem is NULL, `needs_human=1`,
reason `model_disagreement`.

Two runs are not enough: for tricky problem statements, any discrepancy
would trigger `needs_human`, and the 40% threshold from `CALIBRATION.md` §4 would be triggered
by design, not because of an actual problem.
