# Screening benchmark: Appenzeller-Herzog_2019 (sampled)

Run 2026-11-07T17:41:39+00:00 with **claude-haiku-5-5** (ThrottledProvider), 854 model call(s), 56.4 minutes, $0.58.

## What this measures

Each record was screened by the application exactly as a reviewer would run it, and the decision compared with the one the original review's human screeners made. Two recalls are reported because the workflow has a human in it:

- **Workflow recall** — relevant records that survived to a human, whether the model said `include` or `unclear`. Only a record the model excluded outright is lost.
- **Strict recall** — relevant records the model called `include` on its own. What it would achieve unsupervised.

## Results

| | Value |
|---|---|
| **Workflow recall** | 53.8% (95% CI 35.5% to 71.2%) |
| **Relevant records lost** | **12** of 26 |
| Strict recall | 11.5% (95% CI 4.0% to 29.0%) |
| Specificity | 77.5% (95% CI 73.2% to 81.3%) |
| Precision | 13.5% |
| Reviewer burden | 24.4% (104 of 426 records) |
| Number needed to read | 7.4 |

## The 12 record(s) this cost

Relevant studies the screener excluded outright. Nothing downstream would ever have seen these. This is the list a reviewer should read before deciding whether the recall above is good enough for their review.

- Wilson's Disease in the United Kingdom and Taiwan<subtitle>I. GENERAL CHARACTERISTICS OF 142 CASES AND PROGNOSIS<break />II. A GENETIC ANALYSIS OF 88 CASES</subtitle>  <https://doi.org/10.1093/qjmed/42.3.619>
- Clinical presentation, diagnosis and long-term outcome of Wilson's disease: a cohort study  <https://doi.org/10.1136/gut.2005.087262>
- Response to different therapeutic approaches in Wilson disease. A long-term follow up study  <https://doi.org/10.1016/s1665-2681(19)31417-6>
- Modality of treatment and potential outcome of Wilson disease in Taiwan: A population-based longitudinal study  <https://doi.org/10.1016/j.jfma.2017.05.008>
- Histologic evolution and long-term outcome of Wilson’s disease  <https://doi.org/10.1097/meg.0b013e328358f7da>
- Prevention of Wilson's Disease in Asymptomatic Patients  <https://doi.org/10.1056/nejm196802152780702>
- Long-term outcome in Serbian patients with Wilson disease  <https://doi.org/10.1111/j.1468-1331.2009.02607.x>
- Long-term follow-up of Wilson Disease: natural history, treatment, mutations analysis and phenotypic correlation  <https://doi.org/10.1111/j.1478-3231.2010.02354.x>
- Recent clinical features of Wilson?s disease with hepatic presentation  <https://doi.org/10.1007/s00535-004-1466-y>
- Hepatic manifestations of Wilson’s disease: 12-year experience in a Swiss tertiary referral centre  <https://doi.org/10.4414/smw.2018.14699>
- Wilson disease: Histopathological correlations with treatment on follow-up liver biopsies  <https://doi.org/10.3748/wjg.v16.i12.1487>
- Serum Transaminases in Children with Wilson???s Disease  <https://doi.org/10.1097/00005176-200410000-00006>

## The corpus these figures come from

| | |
|---|---|
| Dataset | Appenzeller-Herzog_2019 (sampled) |
| Records | 426 |
| Relevant | 26 (6.1%) |
| Without an abstract | 107 (25.1%) |
| Gold-standard label | `label_included` |
| Licence | not recorded |
| Source review | https://doi.org/10.1111/liv.14179 |

> Only 26 relevant records. The confidence intervals above are wide for a reason, and a point estimate from this benchmark should not be quoted on its own.

## Agreement between the two AI screeners

Cohen's κ = 0.76 over 426 record(s). This says how consistently the two screening passes agreed with each other, which is a different thing from whether either was right.

## The protocol they were judged against

```
Review: Comparative effectiveness of common therapies for Wilson disease: A systematic review and meta-analysis of controlled studies (Appenzeller-Herzog et al., Liver International, 2019).

Eligibility criteria (Section 2.1, verbatim):

We included WD patients of any age or stage. The study drug had to be one of four established therapies, namely DPen, trientine, TTM or Zn. The control could be placebo, no treatment or any other treatment that does not include the respective study drug (eg Zn vs trientine was allowed, Zn 50 mg vs Zn 100 mg was not allowed). Concomitant therapies had to be identical in the compared treatment arms (eg trientine plus Zn vs TTM plus Zn). Comparisons between monotherapy and combination therapy regimens that included the respective monotherapy drug (eg DPen plus Zn vs Zn) have been analysed elsewhere and were not considered any further here. We included studies that reported all‐cause mortality, orthotopic liver transplantation (OLT), neurological symptoms (eg dystonia, dysarthria, cognitive decline, drooling, tremor, gait disturbance, chorea, seizure, psychosis), liver‐related symptoms (eg icterus, ascites, steatosis, fibrosis, mild hepatitis, acute liver failure, cirrhosis, serum transaminases), adverse effects (eg dermatological manifestations, nephrotoxicity, pulmonary toxicity, autoimmune disorders, anaemia, neutrophilic agranulocytosis, thrombocytopenia, hypothyroidism, liver dysfunction, colitis, status dystonicus, myasthenia gravis, arthropathy, macromastia, early neurological deterioration, gastrointestinal irritation), and frequency of treatment discontinuation (ie switching to another drug, stopping or changing the treatment). We included prospective and retrospective studies, including randomized, non‐randomized controlled trials and comparative observational studies that were written in English, German, Dutch, French, Spanish or Portuguese. Animal studies, case reports, case series, cross‐sectional studies, before‐after studies, reviews, letters, abstract‐only publications, editorials, diagnostic or other testing studies and non‐controlled studies were excluded. No publication date restrictions were applied.

Abbreviations as defined in the review: WD, Wilson disease; DPen, D-penicillamine; TTM, tetrathiomolybdate; Zn, zinc salts.
```

## Caveats

- 'label_included' is the post-full-text decision. A record correctly kept at abstract screening and later excluded on full text counts here as a false positive, so specificity is understated against this label.
- 5 row(s) had no title and were dropped.
- 676 of 2868 records (24%) have no abstract. Those are screened on their title alone, which bounds what any screener can do with them.
- Stratified sample: all 26 relevant record(s) kept, 400 of 2842 irrelevant sampled with seed 20260101. Recall is therefore computed over every relevant record in the dataset; specificity and burden are estimates over the sample, and their intervals reflect that. Prevalence in the sample is not the prevalence of the corpus.

## What this does not show

- It is one corpus in one field. A screener that does well on clinical abstracts may do badly on qualitative social science, and nothing here speaks to that.
- The protocol above was written by the person running the benchmark from the published review. A differently worded protocol would produce different numbers, and that variation has not been measured here.
- The human labels are themselves one review team's decisions, not ground truth. Published screening decisions disagree between teams more often than is comfortable.
- Nothing here measures the rest of the pipeline: extraction, synthesis, the framework or the gap register are not evaluated by this run.
