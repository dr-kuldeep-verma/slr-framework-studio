# Screening benchmark: Hall_2012 (sampled)

Run 2026-11-07T20:16:49+00:00 with **claude-haiku-5-5** (ThrottledProvider), 1008 model call(s), 69.2 minutes, $0.61.

## What this measures

Each record was screened by the application exactly as a reviewer would run it, and the decision compared with the one the original review's human screeners made. Two recalls are reported because the workflow has a human in it:

- **Workflow recall** — relevant records that survived to a human, whether the model said `include` or `unclear`. Only a record the model excluded outright is lost.
- **Strict recall** — relevant records the model called `include` on its own. What it would achieve unsupervised.

## Results

| | Value |
|---|---|
| **Workflow recall** | 95.2% (95% CI 89.2% to 97.9%) |
| **Relevant records lost** | **5** of 104 |
| Strict recall | 15.4% (95% CI 9.7% to 23.5%) |
| Specificity | 98.0% (95% CI 96.1% to 99.0%) |
| Precision | 92.5% |
| Reviewer burden | 21.2% (107 of 504 records) |
| Number needed to read | 1.1 |

## The 5 record(s) this cost

Relevant studies the screener excluded outright. Nothing downstream would ever have seen these. This is the list a reviewer should read before deciding whether the recall above is good enough for their review.

- prediction of fault count data using genetic programming  <https://doi.org/10.1109/inmic.2008.4777762>
- Predictive data mining model for software bug estimation using average weighted similarity  <https://doi.org/10.1109/iadcc.2010.5422923>
- A Comparative Evaluation of Using Genetic Programming for Predicting Fault Count Data  <https://doi.org/10.1109/icsea.2008.9>
- Using machine learning for estimating the defect content after an inspection  <https://doi.org/10.1109/tse.2004.1265733>
- Visualization of test information to assist fault localization  <https://doi.org/10.1145/581396.581397>

## The corpus these figures come from

| | |
|---|---|
| Dataset | Hall_2012 (sampled) |
| Records | 504 |
| Relevant | 104 (20.6%) |
| Without an abstract | 0 (0.0%) |
| Gold-standard label | `label_included` |
| Licence | not recorded |
| Source review | https://doi.org/10.1109/TSE.2011.103 |

## Agreement between the two AI screeners

Cohen's κ = 0.51 over 504 record(s). This says how consistently the two screening passes agreed with each other, which is a different thing from whether either was right.

## The protocol they were judged against

```
Review: A Systematic Literature Review on Fault Prediction Performance in Software Engineering (Hall et al., IEEE Transactions on Software Engineering, 2012). Aim (Section 2.1, verbatim): "The aim of this systematic literature review is to analyze the models used to predict faults in source code."

Inclusion criteria (Section 2.2 and Table 2, verbatim):

To be included in this review, a study must be reported in a paper published in English as either a journal paper or conference proceedings. The criteria for studies to be included in our SLR are based on the inclusion and exclusion criteria presented in Table 2.

Before accepting a paper into the review, we excluded repeated studies. If the same study appeared in several publications, we included only the most comprehensive or most recent.

Included papers were published between January 2000 and December 2010.

Table 2. Inclusion and Exclusion Criteria
Inclusion criteria (a paper must be...):
- An empirical study
- Focused on predicting faults in units of a software system
- Faults in code is the main output (dependent variable)
Exclusion criteria (a paper must not be...):
- Focused on: testing, fault injection, inspections, reliability modelling, aspects, effort estimation, debugging, faults relating to memory leakage, nano-computing, fault tolerance.
- About the detection or localisation of existing individual known faults.
```

## Caveats

- 'label_included' is the post-full-text decision. A record correctly kept at abstract screening and later excluded on full text counts here as a false positive, so specificity is understated against this label.
- Stratified sample: all 104 relevant record(s) kept, 400 of 8689 irrelevant sampled with seed 20260101. Recall is therefore computed over every relevant record in the dataset; specificity and burden are estimates over the sample, and their intervals reflect that. Prevalence in the sample is not the prevalence of the corpus.

## What this does not show

- It is one corpus in one field. A screener that does well on clinical abstracts may do badly on qualitative social science, and nothing here speaks to that.
- The protocol above was written by the person running the benchmark from the published review. A differently worded protocol would produce different numbers, and that variation has not been measured here.
- The human labels are themselves one review team's decisions, not ground truth. Published screening decisions disagree between teams more often than is comfortable.
- Nothing here measures the rest of the pipeline: extraction, synthesis, the framework or the gap register are not evaluated by this run.
