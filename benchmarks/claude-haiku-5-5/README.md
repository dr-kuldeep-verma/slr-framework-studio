# Screening benchmark: Claude Haiku 5.5, 7 November 2026

The three runs reported in Section 8 of the paper describing SLR Framework
Studio v0.1.2.

- `wilson.*`, `ptsd.*`, `fault.*`: the harness reports (Markdown to read,
  JSON for tables), including every relevant record the screener lost.
- `protocols/`: the eligibility criteria each run was judged against,
  transcribed from the published reviews.

Data: the classic SYNERGY collection (version 1.0, doi:10.34894/HE6NAQ),
built with `synergy get -v title,abstract_original`. The reconstructed
abstracts are not included here: the SYNERGY terms do not allow them to be
republished as text. To repeat a run, rebuild the CSVs and see
docs/BENCHMARKING.md.

Every run: all finally included records plus 400 others sampled with seed
20260101; model claude-haiku-5-5 via the Anthropic API (no temperature sent;
the model refuses one); answers up to 2,000 tokens; scored against
label_included, the post-full-text decision.
