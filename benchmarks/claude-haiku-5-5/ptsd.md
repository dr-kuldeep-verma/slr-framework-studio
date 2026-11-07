# Screening benchmark: van_de_Schoot_2018 (sampled)

Run 2026-11-07T19:05:33+00:00 with **claude-haiku-5-5** (ThrottledProvider), 883 model call(s), 61.8 minutes, $0.65.

## What this measures

Each record was screened by the application exactly as a reviewer would run it, and the decision compared with the one the original review's human screeners made. Two recalls are reported because the workflow has a human in it:

- **Workflow recall** — relevant records that survived to a human, whether the model said `include` or `unclear`. Only a record the model excluded outright is lost.
- **Strict recall** — relevant records the model called `include` on its own. What it would achieve unsupervised.

## Results

| | Value |
|---|---|
| **Workflow recall** | 97.4% (95% CI 86.5% to 99.5%) |
| **Relevant records lost** | **1** of 38 |
| Strict recall | 42.1% (95% CI 27.9% to 57.8%) |
| Specificity | 81.8% (95% CI 77.7% to 85.2%) |
| Precision | 33.6% |
| Reviewer burden | 25.1% (110 of 438 records) |
| Number needed to read | 3.0 |

## The 1 record(s) this cost

Relevant studies the screener excluded outright. Nothing downstream would ever have seen these. This is the list a reviewer should read before deciding whether the recall above is good enough for their review.

- Resource loss, resource gain, and psychological resilience and dysfunction following cancer diagnosis: A growth mixture modeling approach.  <https://doi.org/10.1037/a0020809>

## The corpus these figures come from

| | |
|---|---|
| Dataset | van_de_Schoot_2018 (sampled) |
| Records | 438 |
| Relevant | 38 (8.7%) |
| Without an abstract | 3 (0.7%) |
| Gold-standard label | `label_included` |
| Licence | not recorded |
| Source review | https://doi.org/10.1080/00273171.2017.1412293 |

> Only 38 relevant records. The confidence intervals above are wide for a reason, and a point estimate from this benchmark should not be quoted on its own.

## Agreement between the two AI screeners

Cohen's κ = 0.52 over 438 record(s). This says how consistently the two screening passes agreed with each other, which is a different thing from whether either was right.

## The protocol they were judged against

```
Review: Bayesian PTSD-Trajectory Analysis with Informed Priors Based on a Systematic Literature Search and Expert Elicitation (van de Schoot et al., Multivariate Behavioral Research, 2018). Aim of the search (Appendix A, verbatim): "The search for articles using LGMM to study the development of PTSD focused on four major databases: Pubmed, Embase, PsychInfo, and Scopus." (LGMM: latent growth mixture modeling.)

Study selection (Appendix A, verbatim):

Following the initial identification of relevant articles, exact duplicates were excluded. After that, there were two rounds of screening for eligibility. In the first round, eligibility was decided by investigating the title and abstract of the article by one author. All relevant papers were screened for the actual application of one of the cluster techniques described in Table A1 within the context of trauma. If there was any doubt, the study was included for the second round. Note that in this stage, we were over-inclusive and during title screening, obviously irrelevant papers (N = 5,822) were removed, for example cross-sectional studies, neurobiological studies and studies from other fields such as studies examining medical procedures after physical trauma.

In the second round, the full-text articles were independently read and screened by RvdS and MS for the following inclusion criteria: (a) longitudinal studies with at least three measurement waves measuring PTSD, (b) studies that measured PTSD on a continuous scale via an interview or questionnaire, (c) and studies that used a clustering method (LGMM, LCGA, hierarchical cluster analysis), (d) traumatic stress symptoms following events that appeared to fulfill DSM-IV criterion A1 for PTSD or acute stress disorder.

Table A1, search terms (verbatim):
Topic terms: Traumatic stress, stress disorder, posttraumatic stress, posttraumatic stress, PTSD
Analysis method terms: Trajectories, latent growth, latent factors, cluster, cluster analysis, latent class analysis, latent growth mixture modeling, mixed linear models, latent profile, mixture, typologies, profile, discriminant analysis
```

## Caveats

- 'label_included' is the post-full-text decision. A record correctly kept at abstract screening and later excluded on full text counts here as a false positive, so specificity is understated against this label.
- 6 row(s) had no title and were dropped.
- 71 of 4538 records (2%) have no abstract. Those are screened on their title alone, which bounds what any screener can do with them.
- Stratified sample: all 38 relevant record(s) kept, 400 of 4500 irrelevant sampled with seed 20260101. Recall is therefore computed over every relevant record in the dataset; specificity and burden are estimates over the sample, and their intervals reflect that. Prevalence in the sample is not the prevalence of the corpus.

## What this does not show

- It is one corpus in one field. A screener that does well on clinical abstracts may do badly on qualitative social science, and nothing here speaks to that.
- The protocol above was written by the person running the benchmark from the published review. A differently worded protocol would produce different numbers, and that variation has not been measured here.
- The human labels are themselves one review team's decisions, not ground truth. Published screening decisions disagree between teams more often than is comfortable.
- Nothing here measures the rest of the pipeline: extraction, synthesis, the framework or the gap register are not evaluated by this run.
