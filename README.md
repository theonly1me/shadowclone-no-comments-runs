# no-comments eval runs

The results and raw data for the [no-comments eval](https://github.com/theonly1me/shadowclone/tree/main/evals/no-comments), a study of whether coding agents write more readable code when they cannot write comments.

**In short:** the comment-free code won 62% of 80 blind pairs (95% interval 53% to 70%). It was clearest for the Claude models and unclear for the two Codex models. The ban did not cost correctness. Read the [eval README](https://github.com/theonly1me/shadowclone/blob/main/evals/no-comments/README.md) for the summary.

## Read the results

- [RESULTS.md](RESULTS.md) has the full method, every result table, and a log of how the study went.
- [EXAMPLES.md](EXAMPLES.md) shows the same code written with and without comments.

## Look at a run

Every one of the 160 runs is in `runs/<setting>/<A or B>/<run>/`, where A means comments allowed and B means comments banned.

| File | What it holds |
| --- | --- |
| `result.json` | Grades for the run: hidden tests, new comments, code measurements, token usage |
| `diff.patch` | The agent's full change to the seed repo |
| `final/` | The files the agent changed or added, as it left them |

`runs/judgments.jsonl` has all 320 blind judge verdicts, and `runs/analysis.json` has every computed result. Agent transcripts and cost figures are not published, because they contain local paths and session details.

## Re-run the analysis

```bash
git clone https://github.com/theonly1me/shadowclone
git clone https://github.com/theonly1me/shadowclone-no-comments-runs
cd shadowclone/evals/no-comments
python -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m analysis.analyze ../../../shadowclone-no-comments-runs/runs
```

This reproduces `runs/analysis.json`.

## License

MIT, the same as Shadowclone.
