# Tone protocol for messages to children (SPEC §7)

Applies to everything the student sees.

## Rules

1. Refer to the text, not the student: "step 2 does not follow from step 1," not "you skipped a step."
2. First name specifically what was done well, and **why** it works—the mechanism,
   not praise.
3. Phrase a flaw as a question: "what must be true for step 2 to follow from step 1?"
4. No more than 2 comments at a time, the most important ones.
5. No longer than 120 words, at the student's reading level.
6. Strict about the standard, neutral in tone. No judgments about the student—only about the text.

## Forbidden patterns (machine-readable)

The block below is read by code (`kurator/reports/tone.py`) and by the test `tests/test_tone.py`.
One source of truth: edit here, and the check changes too.

```forbidden
!                                              # exclamation mark
[\U0001F300-\U0001FAFF☀-➿⬀-⯿]  # emoji
\byou('re| are)\s+(inattentive|lazy|stupid|careless|sloppy|absent-minded)  # judgment of the student, not the text
\bjust\b                                       # belittling the difficulty
\bobvious(ly)?\b                               # belittling the difficulty
\b(is|it's|that's|so) (easy|simple)\b(?! to read)  # belittling the difficulty
\b(well done|good job|smart|genius|super|great job)\b  # label instead of mechanism
\b(yeah right|as always|of course)\b            # sarcasm
\bagain\b                                      # reproach for repetition
```
