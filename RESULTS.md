# No-comments eval: results and method

This is the full write-up of the [no-comments eval](https://github.com/theonly1me/shadowclone/tree/main/evals/no-comments) from Shadowclone: the exact method, every result table, and a log of how the study went. For the short version, read the [eval README](https://github.com/theonly1me/shadowclone/blob/main/evals/no-comments/README.md). To see what the code looked like with and without comments, read the [code examples](EXAMPLES.md).

The eval is not part of Shadowclone's preference benchmarks. No Shadowclone profile, skill, or instruction was used in any run.

## Design

One task, four model settings, two conditions, twenty runs per cell in two batches of ten. That is 160 runs, 320 judge calls, and 80 judged pairs.

| Setting | Agent CLI | Model | Effort |
| --- | --- | --- | --- |
| Sonnet 5.5, high | Claude Code 2.1.289 | `claude-sonnet-5-5` | high |
| Opus 5.5, medium | Claude Code 2.1.289 | `claude-opus-5-5` | medium |
| GPT 6 Luna, high | Codex CLI 0.159.0 | `gpt-6-luna` | high |
| GPT 6.1 Sol, medium | Codex CLI 0.159.0 | `gpt-6.1-sol` | medium |

| Condition | Prompt |
| --- | --- |
| A, comments allowed | The [task prompt](https://github.com/theonly1me/shadowclone/blob/main/evals/no-comments/task/prompt_base.md). It never mentions comments. |
| B, comments banned | The same prompt plus one sentence: "Do not write any comments in code. This includes # comments and docstrings." ([file](https://github.com/theonly1me/shadowclone/blob/main/evals/no-comments/task/ban_suffix.md)) |

Run order was shuffled with a fixed seed within each batch and interleaved across all cells, four runs at a time. Batch 1 (repetitions 1 to 10) ran on 2026-10-04 between 20:36 and 21:21 UTC. Batch 2 (repetitions 11 to 20) started at 21:59 UTC and the last run started at 00:22 UTC on 2026-10-05, because the machine slept for part of it. In batch 1 the mean run time was 65 seconds for Sonnet, 86 for Opus, 194 for Luna, and 212 for Sol. Run times in batch 2 are not reliable for the reason given below.

### The task

The agent works in `ledger`, a small subscription billing library in the [seed repo](https://github.com/theonly1me/shadowclone/tree/main/evals/no-comments/task/seed_repo): 7 modules, 15 passing tests, all money in integer cents. The prompt asks for usage-based billing with mid-cycle plan changes: idempotent usage events, late events, proration with half-up rounding, plan-change validation, overage with floored allowances, and a fixed order of discount, credit, and tax. The prompt states 12 rules, so every behavior the hidden tests check is specified. The agent must also keep the existing tests passing, add tests, and run the suite.

The seed code has 3 comments. Both conditions start from the same code.

### Isolation

Every run gets a fresh copy of the seed repo in a new temporary directory, initialized as a git repository with a baseline commit. Nothing carries over between runs. The hidden tests are not in the workspace during the run. They are copied in after the agent exits.

**Claude Code runs** use the default system prompt and these flags: `-p`, `--model`, `--effort`, `--setting-sources ""`, `--disable-slash-commands`, `--strict-mcp-config`, `--no-session-persistence`, `--output-format stream-json --verbose`, `--allowedTools Bash,Edit,Write,Read,Glob,Grep`, and a settings override that asks for the Bash sandbox. Auto-memory is switched off with `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`. The start-up event of each run shows no skills, no MCP servers, and only built-in plugins. A probe prompt found no instruction file in context. Whether the Bash sandbox was enforced was not verified.

**Codex runs** use `codex exec -s workspace-write` with a dedicated `CODEX_HOME` that holds only a copy of the login file and a one-line `config.toml` (`personality = "none"`). The probe found no instruction files, and only the skills Codex bundles itself (image generation, OpenAI docs, skill creator, skill installer). All Codex runs share that one home and start new sessions.

The [agent invocation code](https://github.com/theonly1me/shadowclone/blob/main/evals/no-comments/harness/agents.py) holds the exact commands. Both CLIs ran under a minimal environment: a clean `PATH` with a Python environment that has pytest, and no inherited variables.

### Grading

1. **Hidden tests.** 50 tests in [graders/hidden_tests](https://github.com/theonly1me/shadowclone/tree/main/evals/no-comments/graders/hidden_tests). A reference solution passes all 50, and the unmodified seed fails them. The tests only use the public API stated in the prompt.
2. **Comment count.** The [counter](https://github.com/theonly1me/shadowclone/blob/main/evals/no-comments/graders/comments.py) uses Python's tokenizer for `#` comments and the parser for docstrings. It counts comments in changed or added files that were not in the baseline version of the same file. A B run with any new comment would count as non-compliant.
3. **Static metrics.** The [measurements](https://github.com/theonly1me/shadowclone/blob/main/evals/no-comments/graders/static_metrics.py) cover changed source files, with comments removed first: code lines, function count, mean and maximum function length, maximum cyclomatic complexity (radon), mean identifier length, maximum nesting depth, and ruff warnings.
4. **Blind pairwise judge.** Within each setting and batch, A run *i* is paired with a randomly chosen B run from the same batch, using each run once, so there are 10 pairs per setting per batch and 20 per setting in total. The judge sees only the source files the agent changed, with all comments and docstrings stripped from both. It never learns which side is which. Each pair is judged in both orders by two judges: Opus 5.5 high through Claude Code and GPT 6.1 Sol high through Codex. That gives 4 verdicts per pair, averaged into one pair score, with a tie counting as half. The judge answers 10 true-or-false claims about each side ([rubric](https://github.com/theonly1me/shadowclone/blob/main/evals/no-comments/graders/judge/rubric.md)) and then picks the side that is easier to read, understand, and change. It is told to ignore correctness.

### Analysis, fixed before the final runs

The first-batch plan was: the primary outcome is the pooled win rate of B over A across all pairs, with a 95% bootstrap interval (10,000 resamples) and a sign-flip permutation test against 50%. Per-setting results are descriptive. Metrics report the mean for A and B and the difference B minus A with a 95% bootstrap interval. All four settings are reported.

### Extension plan, written before the second batch ran

After the first batch of 40 pairs the pooled result was 59% (95% interval 48% to 71%, p = 0.164), which is not significant. A second batch of 10 runs per cell (repetitions 11 to 20, 80 more runs) is run to narrow the interval. These rules were fixed before any batch 2 run started:

- The task, prompts, hidden tests, rubric, judges, and analysis code do not change. The runner only gains a start-repetition option, and pairing and analysis gain batches. Batch 1 pairs and numbers are unchanged.
- Pairs are formed inside each batch, so batch 1 has 40 pairs, batch 2 has 40 pairs, and the combined study has 80 pairs.
- Batch 1, batch 2, and the combined result are all reported.
- The combined result is the headline. The extension followed a look at batch 1, so its p-value is somewhat optimistic. Batch 2 alone is a fresh check of the batch 1 trend.
- There is no third batch, whatever the outcome.

## Results

### Blind judge

| Setting | Pairs | B judged easier to read | 95% interval | p | Batch 1 | Batch 2 | Rubric claims true, A / B |
| --- | ---: | ---: | --- | ---: | ---: | ---: | --- |
| **All four settings** | 80 | **61.9%** | 53% to 70% | 0.009 | 59% | 64% | 34% / 38% |
| Sonnet 5.5, high | 20 | 69% | 51% to 85% | 0.069 | 68% | 70% | 35% / 44% |
| Opus 5.5, medium | 20 | 75% | 59% to 89% | 0.010 | 85% | 65% | 40% / 44% |
| GPT 6 Luna, high | 20 | 54% | 39% to 69% | 0.760 | 45% | 62% | 25% / 26% |
| GPT 6.1 Sol, medium | 20 | 50% | 34% to 66% | 1.000 | 40% | 60% | 34% / 36% |

"B judged easier to read" is the share of pair-order-judge verdicts that preferred the comment-banned code, averaged per pair. 50% means no difference. The first row is the headline: 61.9% across all 80 pairs. The p-value is a sign-flip permutation test against 50%.

- **Batch 1 alone:** 59% (48% to 71%, p = 0.164). **Batch 2 alone:** 64% (53% to 76%, p = 0.027). The second batch was planned before it ran and had not been seen, so it is a fresh check of the first. It points the same way.
- **By judge, all pairs:** Opus 5.5 as judge picked B 58% of the time (51% to 66%), and GPT 6.1 Sol as judge picked B 66% of the time (58% to 73%).
- **Judge reliability:** the two judges agreed on 119 of 160 pair-order verdicts (74%). The same judge gave the same answer after the order was swapped in 127 of 160 cases (79%). The first-shown side won 52% of the time, so there is no sign of a position bias. Two of 320 verdicts were ties.
- **Per setting:** all four settings were above 50% in batch 2 (Sonnet 70%, Opus 65%, Luna 62%, Sol 60%). In batch 1 Luna and Sol were below 50% and Opus was at 85%, so the batch 1 differences between settings were partly noise. With 20 pairs per setting, the intervals stay wide, and the data cannot say whether Luna or Sol benefit.

### Comments written and correctness

| Setting | New source comments, A | New source comments, B | New test comments, A | Hidden tests passed, A / B | Runs obeying the ban |
| --- | ---: | ---: | ---: | --- | ---: |
| Sonnet 5.5, high | 6.2 | 0 | 16.6 | 1000/1000 / 1000/1000 | 20 of 20 |
| Opus 5.5, medium | 6.2 | 0 | 12.9 | 1000/1000 / 1000/1000 | 20 of 20 |
| GPT 6 Luna, high | 2.0 | 0 | 0.2 | 997/1000 / 998/1000 | 20 of 20 |
| GPT 6.1 Sol, medium | 1.3 | 0 | 1.2 | 1000/1000 / 1000/1000 | 20 of 20 |

Comment columns are the mean per run. Five Luna runs missed one hidden test each, three in A and two in B. All other runs passed all 50. Five of 20 Luna runs and 4 of 20 Sol runs in condition A wrote no new source comments.

### Code measurements

Difference B minus A over all 40 runs per setting, mean with 95% interval.

| Setting | Max complexity | Max function length | Code lines |
| --- | --- | --- | --- |
| Sonnet 5.5, high | -1.7 (-2.8 to -0.5) | -4.5 (-7.8 to -1.3) | +3.6 (-7.1 to +14.7) |
| Opus 5.5, medium | -1.0 (-2.2 to +0.2) | +1.2 (-1.1 to +3.5) | +4.2 (-4.0 to +12.5) |
| GPT 6 Luna, high | +0.1 (-1.4 to +1.6) | +0.1 (-5.1 to +5.4) | -16.1 (-33.2 to +1.2) |
| GPT 6.1 Sol, medium | +0.3 (-0.3 to +0.9) | -0.4 (-1.8 to +1.1) | +7.9 (+0.8 to +15.1) |

All measures are in [runs/analysis.json](runs/analysis.json), for all runs and for each batch. Of the 44 intervals for measures other than comment count, 7 exclude zero, where about 2 would by chance. For Sonnet, B had a lower maximum complexity and a shorter longest function. For Sol, B had 7.9 more code lines, slightly longer identifiers (+0.2 characters), and 0.35 fewer ruff warnings. For Luna, only run time excluded zero, and run time is not reliable (see below).

## How the study went

- A pilot of 8 runs (one per cell) checked isolation and the harness. All 8 passed all hidden tests, so the task has no correctness headroom. I kept the task unchanged and relied on readability as the main outcome. The pilot runs are not part of the results.
- Batch 1 completed with no timeouts and no infrastructure failures. One judge verdict could not be parsed, so it was removed and that single call was run again.
- While batch 1 judging was still running, I looked at interim win rates. No decision changed because of that look.
- After batch 1 the result was not significant, so I wrote the extension plan above, committed it, and only then started batch 2. The runner gained a start-repetition option, and pairing and analysis gained batches. Re-running the analysis after those edits reproduced every batch 1 number.
- During batch 2 the laptop, on battery, went into repeated 15 minute sleep cycles overnight, and four Codex runs were stalled. A wake lock (`caffeinate -dimsu`) fixed it. Four runs (Sol A 20, Sol B 14, Sol B 15, Luna B 18) overlapped the sleep, finished normally, and were graded normally. None timed out. I kept them without a rerun. Their recorded run times are wrong, and the timer cannot be trusted for batch 2, so the run-time measure is not used in any claim.
- Batch 2 had no timeouts and no infrastructure failures. One batch 2 judge verdict could not be parsed, so it was removed and that single call was run again.
- The harness binary lookup and temporary directory were made portable after batch 1. Behavior did not change, and rerunning the analysis on the published results reproduces every number.
- Codex printed a warning in some runs that an unauthenticated connector had quit. It had no effect on the work.

## Limits

- One task, one codebase, twenty runs per cell. The pooled interval spans 53% to 70%, and the effect is about 12 points above a coin flip.
- The combined p-value follows an extension decided after seeing batch 1, so it is somewhat optimistic. Batch 2 alone is the cleaner check.
- The judges are language models. Two judges from different families agreed on 74% of verdicts, not all.
- The seed code has 3 comments, and Claude Code's default prompt tells the model to match the comment density of the surrounding code. Both may pull condition A toward more comments.
- Settings differ in reasoning effort, so compare A against B inside a setting, never across settings.
- Condition B adds an instruction, so the study cannot separate the effect of banning comments from the effect of adding any extra instruction.
- The hidden tests are published in the Shadowclone repository, so a future model could have seen them.
