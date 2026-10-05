# Code examples

Two judged pairs from the study, one where the ban looked like it helped and one where it did not. The numbers are in the [results page](RESULTS.md#results).

## How the pairs were chosen

For each setting, the pair whose judge score is closest to that setting's average, ties broken by the lowest run number. The rule was applied to the first batch of 40 pairs, after its results and before the second batch ran, to avoid picking a flattering pair by hand. The setting averages below are the batch 1 averages. A pair score is the share of its 4 judge verdicts that preferred the banned-comment code.

| Setting | Setting average | Chosen pair | Pair score |
| --- | ---: | --- | ---: |
| Opus 5.5, medium | 85% | A run 03 against B run 10 | 75% |
| GPT 6 Luna, high | 45% | A run 06 against B run 05 | 50% |

The judges saw both versions with all comments and docstrings removed. The snippets below keep the comments, to show what the models wrote. Full files are under `runs/<setting>/<A or B>/<run>/final/ledger/`.

## Opus 5.5, medium: A run 03 against B run 10

Run 03 in condition A wrote 6 new source comments and 21 new test comments. Run 10 in condition B wrote none. Three of the four judge verdicts preferred the B code.

### The same step, with and without its comment

Both runs pick the usage events to bill. The A run explains the rule above the list. The B run uses the same list with no explanation.

Condition A, `billing.py`:

```python
        # Bill every unbilled event that happened before this period ends.
        events = [
            event
            for event in self._store.list_usage_events(subscription_id)
            if event.billed_invoice_id is None
            and event.occurred_on < subscription.period_end
        ]
```

Condition B, `billing.py`:

```python
        events = [
            event
            for event in self._store.list_usage_events(subscription_id)
            if event.invoice_id is None and event.occurred_on < subscription.period_end
        ]
```

The code is almost identical. Here the comment only repeats what `occurred_on < subscription.period_end` already says.

### A rule that the comment carried and the B code only implies

The prompt says a late event, one dated before the open period, bills in the first segment. The A run states this in a comment.

Condition A, `invoices.py`:

```python
    for event in events:
        # Late events for a closed period fall into the first segment.
        target = segments[0]
        for segment in segments:
            if segment.start <= event.occurred_on < segment.end:
                target = segment
                break
        target.units += event.units
```

Condition B, `billing.py`:

```python
        units = [0] * len(plan_ids)
        for event in events:
            index = 0
            for i in range(1, len(plan_ids)):
                if event.occurred_on >= boundaries[i]:
                    index = i
            units[index] += event.units
```

In B the rule appears only as `index = 0`. The loop also uses a single-letter `i` and parallel lists, and one judge noted the parallel lists. The ban did not make this part clearer.

### What the judges said

The four reasons are about structure, not comments. They preferred B for a named `PlanChange` type kept in storage instead of tuples on the subscription, and for helper functions that name the proration and allowance rules. The one verdict for A preferred its segment objects over B's parallel arrays.

This is one pair. It cannot tell whether the ban caused those structural choices or whether two runs of the same model simply differ.

### A comment that is not new

The B run still contains `# The next period has the same length as the one just billed.` That line came from the seed repo, so it does not count as a new comment. The ban applies to what the model writes.

## GPT 6 Luna, high: A run 06 against B run 05

Run 06 in condition A wrote no new comments at all. Its only two comments, `# Credit is spent after the discount and before tax.` and `# The next period has the same length as the one just billed.`, came from the seed. So this pair compares two runs that both wrote no comments, and the judges split 50%.

Luna and Sol wrote few comments when free to. The mean in condition A was 1.9 for Luna and 1.0 for Sol, and 2 of 10 Luna runs and 3 of 10 Sol runs wrote none. For them the ban removed only one or two comments per run, which fits the lack of a difference in the judge results.
