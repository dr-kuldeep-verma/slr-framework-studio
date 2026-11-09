# Pipeline benchmark plan: Wilson disease, full text to framework

Fixed on 8 November 2026, before any full-text model call. Changes after this
date are listed at the end with their reason.

## Question

When the stages after title/abstract screening are run by Claude Haiku 5.5 on
the open-access full texts of the Wilson disease review (Appenzeller-Herzog
et al., 2019), how close do the AI first passes come to what the review's
authors decided and extracted?

## Records

The 134 records that would reach full text in a real review: all 26 records
the review finally included, and the 108 other records that the
title/abstract benchmark of 7 November 2026 sent to a human (include,
unclear, conflict or audit). Only open-access copies are used, found through
OpenAlex and Europe PMC and downloaded by `fetch_oa.py` on the author's
computer. Records without one are marked "not retrieved: no open-access copy".

## The scripted reviewer

The app asks a person to decide at several points. Here a script decides,
by one rule fixed in advance:

* it accepts every AI proposal unchanged (codebook, label mappings,
  orientations as proposed, themes, propositions, extraction records), and
  says in the logged reason that it did so unread;
* except where the gold standard answers the question directly: it settles
  full-text eligibility by the review's final inclusion, and it confirms a
  shared-dataset link only when both records belong to the same study in
  Table 1 of the review.

Each later stage therefore starts from correct inputs, and each AI first pass
is scored on its own. Errors do not compound, which is reported as a
limitation: a real reviewer may miss some of what the script corrects.

## Gold standard

`wilson_gold.json`, transcribed by hand from Table 1 (study design,
treatment arms, outcomes, patients analysed and original sample size) and
Table 2 (pooled odds ratios) of the review.

## Measures

Full-text eligibility (retrieved records only):
* recall and specificity of the AI decision (include or unclear counts as
  kept) against final inclusion, with Wilson 95% intervals;
* records lost (excluded relevant records), listed;
* quotation check: share of decisions whose quotation was found on the cited
  page, elsewhere, or nowhere;
* share of records sent to a human.

Extraction (included studies with a retrieved report; one study may have
several reports, and a study counts as covered if any report covers it):
* design family correct: randomised controlled trial = randomised_experiment;
  non-randomised controlled trial = quasi_experiment; retrospective or
  prospective cohort = longitudinal;
* sample size: the extracted number equals the review's analysed or original
  sample size;
* treatment arms found: each gold arm named in any relationship label, the
  design text or the key findings (keyword lists in `pipeline.py`);
* outcomes found: each gold outcome named in any relationship's outcome
  label (keyword lists in `pipeline.py`);
* quotations found on the cited page; direction re-check agreement; share of
  relationships and reports queued for a human.

Synthesis and framework:
* codebook coverage: each gold arm and outcome has a construct whose name or
  definition matches its keywords;
* shared datasets: AI-proposed links scored against same-study pairs
  (precision and recall over record pairs);
* for each pooled finding in Table 2, whether a graded link connects a
  matching treatment construct to a matching outcome construct, and its
  direction and grade, listed in full for the reader to judge;
* themes and propositions: counts, and the share of propositions whose every
  citation resolves to a graded link.

## Budget

US$10 across all stages, shown as an estimate before the first call.

## Deviations

None yet.
