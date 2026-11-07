# Benchmarking the screener

The application has over 1,400 tests. Not one of them says whether it
finds the right studies. They prove it behaves as designed; this says
whether the design works, which is a different question and the only one a
journal will ask.

Running a benchmark answers it: screen a corpus whose human decisions are
already published, and compare.

## The short version

```bash
# 1. Get a labelled dataset (SYNERGY format: title, abstract, label).
# 2. Write the review's eligibility criteria into a text file.
# 3. Check the harness works, free, against the mock model:
slr benchmark data/Appenzeller-Herzog_2020.csv \
    --protocol protocols/wilson.txt \
    --sample-irrelevant 400 --mock

# 4. Then for real. The API key is the one saved in the application under
#    Tools > Model settings; the command shows the estimated tokens and cost
#    and asks before it spends anything.
slr benchmark data/Appenzeller-Herzog_2020.csv \
    --protocol protocols/wilson.txt \
    --sample-irrelevant 400 \
    --provider anthropic --model claude-sonnet-5-5 \
    --budget 20 \
    --licence "CC-BY 4.0" \
    --reference "https://doi.org/10.1111/liv.14179" \
    --out results/wilson.md
```

You get `wilson.md` to read and `wilson.json` to put in a table. Name the
model with `--model` for any run you will report: the default changes as
providers release new models, and a result is a property of one model.

## Where the datasets come from

The [SYNERGY collection](https://github.com/asreview/synergy-dataset)
publishes 26 systematic reviews: every record the search returned, and the
decisions the human screeners made on it. Its own tool builds the CSVs:

```bash
pip install synergy-dataset
synergy list                       # the dataset names
set SYNERGY_SET=synergy            # Windows cmd; `export` elsewhere
synergy get -d Appenzeller-Herzog_2020 -o data
```

`SYNERGY_SET=synergy` selects the classic collection (DOI
10.34894/HE6NAQ), which keeps every record the search found, including
those with no abstract. Without it the tool builds SYNERGY+, which keeps
only open-access records with an abstract: a smaller, easier corpus, and a
different one, so say which you used. The tool asks you to acknowledge that
abstracts are rebuilt from an index and may not be republished as text;
answer yes, and do not commit the CSVs to a public repository.

The loader reads the title/abstract label under either name SYNERGY has
used for it (`label_abstract_included` now, `label_abstract_screening`
before). Other collections in the same shape work too: the CLEF eHealth TAR
sets, Cohen's drug-review datasets.

## Three things that decide whether a benchmark is fair

**Use the right label.** The CSVs carry two. `label_abstract_screening` is
what the reviewers decided from the title and abstract.  `label_included`
is what survived full-text assessment as well. Scoring a title/abstract
screener against the second punishes it for every study that looked
eligible and turned out not to be — which was the *correct* call at that
stage. On the first dataset we tried they differ by a factor of six: 178
records against 29. The loader picks the abstract-screening label
automatically and records which one it used.

**Write the protocol from the published review, not from the data.**
Eligibility wording drives screening more than almost anything else, so a
protocol tuned until the tool passes is not a benchmark. The harness
refuses to run without one and prints it in the report, so a reader can
judge what the model was actually asked. Take it from the review's methods
section.

**Report the corpus, not just the score.** A third of the records in a
typical dataset have no abstract at all — a title and nothing else. That
bounds what any screener can do and has nothing to do with the tool. Every
report states it.

## On a free API tier

Free tiers cap requests per minute and per day, and a benchmark makes
thousands. Three options make that workable:

- `--requests-per-minute N` paces the calls. Set it a little under the
  tier's per-minute limit, so no request is wasted on a refusal.
- `--workspace FOLDER` keeps the run on disk. When the daily quota runs
  out the run pauses, writes no report, and says so; the same command the
  next day carries on where it stopped. Answers already received are
  cached and never paid for twice, and screener agreement is computed over
  the whole run, not the last day of it.
- `--max-tokens N` raises the length allowed per answer. Models that think
  before answering spend tokens doing so; if answers come back empty, raise
  it.

Gemini, through its OpenAI-compatible endpoint:

```bash
slr benchmark data/Appenzeller-Herzog_2020.csv \
    --protocol protocols/wilson.txt --sample-irrelevant 400 \
    --provider openai_compatible \
    --base-url https://generativelanguage.googleapis.com/v1beta/openai \
    --model <the exact model id from Google AI Studio> \
    --requests-per-minute 8 --max-tokens 2000 \
    --workspace work/wilson --out results/wilson.md
```

The key is entered under Tools > Model settings as the OpenAI-compatible
endpoint's key. No price is on file for these models, so the estimate gives
tokens but not money. Two things to report: that a free tier was used, and
that a free tier's terms may let the provider use what is sent to it.

## What gets measured, and why these

| Figure | What it means |
|---|---|
| **Workflow recall** | Of the relevant records, how many survived to a human — `include` **or** `unclear`. The headline, because this workflow has a human in it and only an outright exclusion is final. |
| **Records lost** | Relevant studies excluded outright, listed by title. The harm, in the only form anyone reads. |
| Strict recall | Relevant records the model called `include` unaided. What it would manage unsupervised. |
| Specificity | Irrelevant records correctly excluded. |
| Reviewer burden | Share of the corpus still reaching a human. The reason to use the tool at all. |
| Number needed to read | Abstracts read per relevant study found. |

Everything carries a Wilson interval. A recall of 100% over 12 relevant
records is not evidence of a perfect screener, and the interval is what
stops it reading as one.

Recall and burden have to be read together. A screener that includes
everything scores perfect recall and saves nobody anything — and that is
exactly what the mock model does, which makes it a useful check that the
report is honest:

```
Workflow recall: 98.9%
Records lost:    2 of 178
Burden:          236 of 238 records reach a human   ← useless
```

## Keeping the cost down

A full dataset is thousands of records, and each costs two screening calls
plus an adjudication where the two disagree. `--sample-irrelevant N` keeps
**every** relevant record and samples N irrelevant ones. Recall stays exact
over the whole corpus — no positive is dropped — while specificity and
burden become estimates over the sample, which their intervals account
for. The sample is deterministic given `--seed`.

400 irrelevant records against a typical 150 relevant gives roughly 1,100
to 1,650 model calls.

## What a benchmark cannot tell you

- **One corpus, one field.** A screener good on clinical abstracts may be
  poor on qualitative social science.
- **The labels are not ground truth.** They are one review team's
  decisions. Published screening decisions disagree between teams more
  often than is comfortable.
- **Only screening.** Extraction, synthesis, the framework and the gap
  register are not evaluated by any of this. They need their own
  benchmarks and do not yet have them.
- **Protocol sensitivity is unmeasured.** Rewording the eligibility
  criteria would change the numbers, and nobody has yet measured by how
  much. That is a study in itself, and arguably a more interesting one.

## If you are writing this up

Report at least three datasets from different fields, every figure with
its interval, the protocol text for each, and the model and date. State
the prevalence and the missing-abstract share of each corpus. Publish the
protocols and the JSON next to the paper so the run can be repeated.

And say that the AI's decisions were confirmed by a human in the workflow
being described, because they were — a recall figure from this harness
describes an AI first pass, not an unsupervised screener.
