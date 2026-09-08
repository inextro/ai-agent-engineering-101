# Week 02 — Harness A/B: ReAct vs Plan-then-Execute

Model, task, and tools are held constant. Only the harness varies.

## How to reproduce

| | |
|---|---|
| Provider | OpenRouter, through the OpenAI-compatible path in `tools_shared.py` |
| `OPENAI_BASE_URL` | `https://openrouter.ai/api/v1` |
| `AGENT_MODEL` | `nvidia/nemotron-3.5-lightning:free` |
| Tools | `read_file(path)`, `count_pattern(path, pattern)` — unchanged from the starter, imported by both harnesses from `tools_shared.py` |
| Task | the `task:` line of `TASK.md`, committed before the first run |
| Success | the `expected:` line `14:00` appears in the final answer (`run_ab.py:judge`) |
| Input | `app.log`, unchanged. Hour 14 has 6 ERROR lines, the maximum; 12 is second with 3. |

```bash
cd submissions/25520014/week-02
export OPENAI_BASE_URL=https://openrouter.ai/api/v1
export OPENAI_API_KEY=<your openrouter key>
export AGENT_MODEL=nvidia/nemotron-3.5-lightning:free
python run_ab.py --runs 3
```

The starter's `.env` model, `minimax/minimax-m3:free`, now returns
`404 This model is unavailable for free`, so the model above is the one the
week-02 README names as tested against this starter. Both harnesses keep the
starter's caps: ReAct `max_steps=8`; Plan-then-Execute `max_replan=1`,
`max_tool_rounds=3`.

## 1. Variant definition — which axes differ

| Axis | ReAct | Plan-then-Execute |
|---|---|---|
| 1. Context management | One `Chat`. Full history accumulates; every tool result stays in the same transcript the model reasons over. | **Two** `Chat`s. The planner runs with `tools=False` and never sees a single tool output; the executor never sees the planner's reasoning, only the serialized plan. They exchange one JSON list, and on a replan one truncated failure string. |
| 2. Tool granularity | Identical — both `import` from `tools_shared`. | Identical. **Held constant**; this is the control that makes the A/B a harness comparison. |
| 3. Termination condition | Model-decided: a reply with no tool call ends the loop. Hard cap `max_steps=8`. | **Plan exhaustion**: the loop runs until `i == len(plan)`, then forces one final-answer call. A mid-plan `Answer:` does not stop it. Two nested caps on top: `max_tool_rounds=3` per step, `max_replan=1`. |
| 4. Error recovery | One level. A tool exception is stringified into the Observation and the model re-decides on the next step. | Two levels. Inside a step, same as ReAct. But a step that cannot proceed emits `OFF_PLAN:`, which escalates to the *planner* to regenerate the remaining steps — once. |
| 5. Human intervention point | A hook exists: `IRREVERSIBLE` names the tools that need `ask_human()` first. It is an empty set, because the starter tools are read-only. | No approval hook anywhere in the harness. |

Axes 1, 3, and 4 differ substantively. Axis 5 differs in structure only —
ReAct has an unused gate and Plan-then-Execute has none — and this task cannot
exercise either, which is why the column below is all zeros. That is a gap in
the experiment, not a tie between the harnesses.

## 2. Measurements

From `results.csv`. Wall-clock is from the `[judge]` line of each log.

| run | harness | success | tokens | iters | interventions | wall clock | note |
|---|---|---|---|---|---|---|---|
| 1 | react | O | 4044 | 2 | 0 | 129.7s | |
| 2 | react | O | 3999 | 2 | 0 | 48.0s | |
| 3 | react | O | 3758 | 2 | 0 | 33.3s | |
| 4 | plan_exec | O | 54307 | 19 | 0 | 425.1s | replans=1 |
| 5 | plan_exec | X | — | — | — | 180.4s | crash: 429 free-models-per-day |
| 6 | plan_exec | X | — | — | — | 1.5s | crash: 429 free-models-per-day |

ReAct mean: 3934 tokens, 2 iterations, 3/3 success.
Plan-then-Execute, on its one completed run: 54307 tokens, 19 iterations —
**13.8× the tokens and 9.5× the iterations** for the same answer.

Runs 5 and 6 are not harness failures. They are the OpenRouter free tier's
50-requests-per-day cap, hit mid-experiment. The cause is still on the axis
being measured, though: one Plan-then-Execute run costs about 9.5× the requests
of one ReAct run, so the third ReAct run and the first Plan-then-Execute run
together consumed the day's budget. The termination condition did not just move
a metric, it ended the experiment.

Two other runs outside `results.csv`, both kept as evidence:

- `logs/react-00-smoke-tail.txt` — a pre-experiment smoke run of
  `harness_react.py`. The model took the `count_pattern` route, spent one
  iteration per hour, and hit `max_steps=8` with `MAX_STEPS reached: incomplete`
  at 21386 tokens. Only the tail was captured.

## 3. Interpretation

**Axis 3 (termination condition) moved tokens and iterations, and it moved them
by an order of magnitude.** The evidence is in `logs/plan_exec-04.txt`: the
executor prints `Answer: 14:00` at step 2 of a five-step plan, and the harness
keeps going — steps 3 and 4 re-derive counts that were already on the
transcript, because "done" is defined as `i == len(plan)`, not as "an answer
exists". ReAct reaches the same answer and stops, in both cases on the second
model call, because there "done" is a reply with no tool call. Same model, same
tools, same file; the 13.8× gap is the definition of "done".

**Axis 1 (context management) is what supplied the work for axis 3 to waste.**
The planner runs with `tools=False`, so it writes a plan without ever seeing
that `read_file` returns all 3022 bytes of `app.log` at once — well inside the
4000-character guard. Not knowing that, it splits reading from counting into
separate steps (`'Read app.log'`, `'Count ERROR occurrences per hour'`), and the
executor, prompted one step at a time, dutifully calls `count_pattern` nine
times, once per hour. ReAct's single accumulating transcript makes that split
impossible to express: the model that reads the file is the model that counts,
in the same turn.

**Axis 4 (error recovery) was the only axis that helped Plan-then-Execute.**
The `max_tool_rounds=3` sub-cap cut the nine-hour enumeration short, the step
reported `OFF_PLAN: step exceeded the tool-call budget`, and the replan dropped
the `'Count ERROR occurrences per hour'` step entirely. The recovery path
produced a *better* plan than the original — a real result, and the one place
where the extra machinery paid for itself.

The honest caveat is that **ReAct's cheapness is not something its harness
guarantees.** Three graded runs took the `read_file` route in two iterations, but
the smoke run took the `count_pattern` route and died at `max_steps=8`. The same
per-hour enumeration that the plan *forces* on the executor, ReAct's model can
also *choose*. When it does, axis 3's cap binds on ReAct too, and the failure is
a hard `MAX_STEPS reached: incomplete` with no recovery path — because axis 4
gives ReAct one level of recovery, not two. So the fair reading of these numbers
is not "ReAct wins": it is that ReAct has the lower floor and the higher
variance, while Plan-then-Execute pays a fixed order-of-magnitude premium to buy
a second recovery level and a bounded, inspectable trajectory.

The measurement this experiment could not make is axis 5. With read-only tools,
`interventions` is structurally 0 in both harnesses and the column carries no
information. Distinguishing them there needs a tool that writes.
