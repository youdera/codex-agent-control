---
name: grill-me
description: Align a consequential task or stress-test a plan before delegating agents. Use for ambiguous goals, changed scope, or an explicit grill-me request; reuse an already agreed contract for routine follow-up work.
---

# Grill me: align once, execute and review against the same brief

Adapted for this controller from Matt Pocock's grill-me / grilling workflow. The source's interview and decision-frontier ideas are credited in the repository's third-party notice and license. This is a local workflow adaptation, not an upstream drop-in copy.

The controller performs alignment. Workers execute the resulting brief; do not make every worker interview the user again.

1. Identify the intended outcome, deliverables, constraints, acceptance evidence and decisions that change implementation. Find environmental facts yourself using targeted reads or bounded investigation; do not ask the user to look them up.
2. Reuse prior explicit user answers and project contracts. Record their basis. Do not reopen settled decisions or ask for permission already granted.
3. Map unresolved decisions by prerequisite. Ask only the currently answerable, material questions, with concise options and a recommendation. A decision that depends on an unanswered question waits. Use a compact round; do not expand into hypothetical branches unrelated to the requested outcome.
4. With an explicit full grill-me request, continue the interview until shared understanding is reached. Preserve the user's decisions and summarize the resulting brief before dependent implementation. Existing explicit authorization/confirmation counts; silence is never an answer.
5. For routine follow-ups with an unchanged, complete contract, use `mode: existing-contract` and proceed within authorization. For a new ambiguous task use `mode: grill-me`; resolve consequential open questions before dispatch.
6. Write the compact `alignment` object in the work order: `mode`, `goal`, `deliverables`, `acceptance`, `constraints`, `decisions`, `open_questions`, `basis`. The queue rejects an explicitly unresolved brief. Never claim alignment based solely on an agent's guess or fabricated user approval.
7. Delegate a complete independently verifiable unit, including this same brief in worker context. At recovery/acceptance compare the actual artifact and evidence to these criteria. A change of goal updates the brief explicitly; it is not a quiet implementation choice.

The software validates structure and unresolved-question lists. The controller remains responsible for truthful alignment and for deciding which questions matter.
