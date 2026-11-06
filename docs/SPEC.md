# Build prompt: SLR Framework Studio (Windows desktop app)

Paste everything below the line into Claude Code (or another coding agent) in an empty project folder.

---

You are building **SLR Framework Studio**, a Windows desktop application (.exe) that takes a researcher from database exports to an evidence-graded theoretical framework. It must follow PRISMA 2020 and keep a full audit trail. AI does the first pass at every stage, and a human confirms or overrides each decision before it counts. Build it in the stages below, run the tests at the end of each stage, and show me a working build before moving on.

## 1. Technology

- **Language:** Python 3.12.
- **User interface:** PySide6 (Qt), with a left-hand stage navigator (Project → Import → Deduplicate → Screen → Retrieve → Full-text → Extract → Synthesise → Framework → Gap Finder → Export) and drag-and-drop file areas.
- **Storage:** one SQLite database per project (`project.slr`), plus a project folder for PDFs and exports. Nothing leaves the computer except calls to the language model the user chooses.
- **Language model:** a pluggable provider layer.
  - Anthropic Claude API is the default.
  - Also support OpenAI-compatible endpoints and local models through Ollama for users who cannot send data to the cloud.
  - Store API keys in the Windows Credential Manager via `keyring`, never in plain text.
  - Use temperature 0 and structured JSON output validated with pydantic schemas.
  - Retry with backoff, and cache each response by a hash of prompt and input so that reruns are free and reproducible.
- **PDF and OCR:**
  - Read text layers with PyMuPDF.
  - Use Tesseract OCR (bundled) when a page has no text layer.
  - Record the page number for every extracted passage.
- **Figures:** straight-line, PowerPoint-style diagrams drawn with reportlab (PDF/SVG/PNG), and an editable `.pptx` copy of each built with python-pptx.
- **Documents:** python-docx for Word outputs and openpyxl for Excel.
- **Packaging:** PyInstaller one-folder build plus an Inno Setup installer. Bundle Tesseract and fonts, and include an app icon and version info.

## 2. Project setup (protocol)

A form collects:

- the review title, objectives and research questions;
- the framework type (PCC, PICO, SPIDER or SOBC/S-O-R);
- inclusion criteria;
- an ordered list of exclusion codes, from cheapest and most objective to most interpretive;
- date and language limits;
- seed studies (DOIs) used to check search recall.

The app then:

- drafts a protocol document from the form;
- locks it with a timestamp and content hash;
- logs every later change in an amendment log with date, reason and whether it was made after data were seen.

## 3. Import

- **Formats:** RIS from Scopus, Web of Science, EBSCO, ProQuest and others; PubMed `.nbib`; EndNote XML; CSV.
- **Normalisation:** map each format's tags to common fields (title, abstract, authors, year, journal, DOI, keywords, language, document type, source database). Keep the raw record unchanged.
- **Display:** show counts per file and per database, and flag records missing an abstract.
- **Seed check:** report which seed studies were found in the imports.

## 4. Deduplication

Records are matched in three steps:

1. exact DOI;
2. normalised title plus year within ±1;
3. fuzzy title similarity at or above a threshold the user can change.

Uncertain pairs go to a side-by-side review screen. Every merge is logged with its rule, and the source databases of merged records are preserved.

## 5. Title and abstract screening

- **Pilot (recommended):** the user labels a random sample of 50 to 100 records; the AI then screens the same sample, and the app reports agreement before a full run is allowed. The user may waive this, but the waiver is logged.
- **Two blinded screeners:** two AI screeners, each with a different reviewer persona, see only the protocol and the record. They return a decision (include, exclude or unclear), the first failing exclusion code and a one-sentence reason quoted from the record.
- **Adjudication:** a third AI call settles include/exclude disagreements.
- **Quality-control re-screen:** a separate pass re-screens a reproducible random sample of joint exclusions, plus exclusions matching keyword signals for the core criteria.
- **Agreement:** report percentage agreement, Cohen's κ and PABAK.
- **Human review queue:** conflicts, unclear records, QC rescues and a random sample of agreed decisions. The human decision always overrides the AI decision.
- **Outputs:** an Excel screening log (one row per record, with every reviewer's decision, code and reason) and live PRISMA counts.

## 6. Full-text retrieval

- **Retrieval list:** DOI links and open-access locations found through the Unpaywall API using the user's email address.
- **Rules:** the app must not download from paywalled sites, use institutional credentials, solve CAPTCHAs or scrape.
- **Uploads:** the user drag-drops PDFs, which are matched to records by DOI, then title. Unmatched files go to a manual matching screen.
- **Tracking:** record each report as retrieved, not retrieved or abstract only, with a reason.

## 7. Full-text eligibility

The AI assesses each PDF against the ordered exclusion codes and returns:

- a decision;
- the exclusion code;
- a page-located quotation supporting the decision.

The app confirms that each quotation actually appears on the cited page (string match after normalisation) and flags any that do not. Borderline decisions and failed quotations go to the human review queue.

## 8. Data extraction

**Per-report schema:**

- country, setting and population;
- design (with one rule: an "experiment" requires a manipulated treatment against a control);
- sampling, sample size and time horizon;
- theories used and their role;
- framework stage coverage;
- key findings and stated limitations;
- an appraisal note with the design-appropriate instrument (AXIS, MMAT, CASP or RoB 2).

**Per-relationship schema:**

- predictor and outcome, exactly as labelled by the authors;
- relationship type, and any mediator or moderator;
- direction, strength or statistic, and significance;
- evidence basis (statistical, thematic or asserted);
- page and a supporting quotation.

**Checks:**

- A separate AI verification pass re-derives the direction of each relationship from its quotation.
- The app checks that every quotation exists in the PDF text.
- Disagreements go to the human queue.

**Second coder:** an optional mode lets a second person code a random sample, and the app reports inter-coder agreement.

## 9. Synthesis engine

- **Codebook:** the AI proposes a construct codebook from the objectives and the extracted labels (definitions, includes, excludes). The human edits and approves it before any mapping.
- **Mapping:** each label is mapped to a construct by what was measured, not by vocabulary. A residual "other" code is allowed, and contestable mappings are flagged.
- **Direction harmonisation:** the human fixes one reference orientation per construct. The app re-expresses every relationship against those orientations and logs every sign flip.
- **Dataset linkage:** detect reports that share a sample, using authors, sites, fieldwork dates and sample size. The human confirms each link. Linked reports vote once, and reviews or conceptual papers do not vote.
- **Grading:** vote counting at dataset level, with configurable rules applied in a fixed order of precedence: asserted, contested, strong, moderate, emerging, tentative. The default strong rule is at least five datasets, none opposing, at least 60% agreeing, a two-sided exact sign test with p < .05 and at least two countries. Every link reports a Wilson 95% confidence interval.
- **Sensitivity analyses:** alternative strong rule; exclusion of high-concern studies; quantitative evidence only; experiments only; observational studies only. Report any link that changes sign or grade.
- **Contradictions:** for each contested link, a table explaining the disagreement by context, measurement, method or sample, with a suggested moderator or construct split for the human to accept or reject.

## 10. Framework builder

- **Evidence maps:** one figure per theme, generated from graded links only. Line weight shows the grade: thick for strong, solid for moderate, dashed for emerging, and double-headed for undirected associations. Each arrow is labelled with its link ID and sign. The figures use straight connectors, aligned boxes, no line passing through a box and as few crossings as possible.
- **Theoretical framework:**
  - The AI drafts propositions, each with its grounding links, a mechanism drawn from theories the user lists, moderators, boundary conditions and a test that could falsify it.
  - The framework keeps three kinds of element separate: evidenced links, proposed mechanisms and untested pathways.
  - Every proposition cites only links that exist in the graded inventory. Reject any output that does not.
- **Integrated diagram:** a conceptual diagram of the full framework in the same visual style, editable in PowerPoint.

## 11. Gap Finder

The Gap Finder identifies, evidences and ranks research gaps. It runs in two modes.

**Quick scan (from RIS records only, before full review).** This mode clusters titles, abstracts and keywords into topics using embeddings and clustering, then shows:

- the trend of each topic over time;
- keyword co-occurrence;
- sparse intersections, such as topic × region or topic × method with few or no records.

The result is a scoping view for refining objectives, not a finding.

**Full analysis (after extraction and synthesis).** This mode builds gaps from the coded evidence, using seven gap types:

- **Evidence gaps:** construct pairs in the codebook or framework with no study, or only tentative evidence (one dataset).
- **Theoretical gaps:**
  - framework stage transitions with no graded link, for example stimulus → organism or behaviour → consequence;
  - mechanisms named by the user's theories but never measured, for example efficacy or perceived threat;
  - untested pathways and feedback loops.
- **Contradiction gaps:** contested links that remain unresolved after the contradiction analysis.
- **Methodological gaps:**
  - designs absent or rare for each construct, such as no experiments, no longitudinal or panel studies, or no observed behaviour;
  - measures used only once;
  - stated intentions never compared with behaviour.
- **Population and context gaps:** cross-tabulation of constructs against country, World Bank income group, region, setting, population type and activity, with empty and thin cells flagged.
- **Information or intervention gaps:** stimulus attributes the user names (for example source, certainty, framing, lead time) that were never varied.
- **Author-stated gaps:** "future research" and "limitations" statements extracted from every included paper, clustered into themes and counted. Each statement keeps its source and page.

**Evidence gap map.** A heat-map matrix (constructs or framework links × settings, regions or designs), with cell counts of datasets and colours for evidence grade. Clicking a cell lists its studies. The map exports to PNG, SVG and Excel.

**Gap statements.** For every gap, the app writes a short statement with its evidence: counts, cell values, links and the studies cited. For example: "No dataset in the corpus measures efficacy together with climate concern (0 of 89 datasets)."

- Every claim of absence is qualified as "within the included corpus, searched to [date]".
- No gap statement may be produced without the counts and records that support it.

**Freshness check.** Before reporting a gap, the app queries OpenAlex and Crossref for papers published after the search date that match the gap's constructs. It reports possible recent work and marks the gap as "possibly being addressed" for the user to check.

**Prioritisation.** Each gap is scored on four criteria, with weights the user can adjust:

- centrality in the framework (how many propositions depend on it);
- size (how empty the cells are);
- practical relevance (whether it appears in author-stated gaps and implications);
- feasibility (whether a standard design could close it).

The user can override any score, and the ranking is shown as a table.

**From gap to study.** For each top-ranked gap, the app drafts:

- a research question;
- a testable hypothesis linked to the relevant proposition;
- a suggested design and measures;
- the population and setting that would close the gap.

These drafts feed directly into the research agenda table and the Future Research section draft.

**Gap Finder exports:**

- a gap register (Excel), with gap type, statement, supporting evidence, freshness flag, priority score and suggested study;
- the evidence gap map figure;
- a research agenda table (Word);
- a Future Research section draft.

## 12. Exports

- PRISMA 2020 flow diagram (Word and PDF) and a pre-filled PRISMA checklist;
- screening log and extraction matrix (Excel);
- RIS files of screened-in and included records;
- a QualCoder-compatible REFI-QDA project (`.qdpx`) containing all codings, validated against the REFI-QDA schema;
- graded inventory, sensitivity tables and contradiction table (Excel and Word);
- figures (PDF, SVG, PNG and PPTX);
- a Methods section draft that describes the procedure exactly as run, including an AI-use statement listing the model, version, date and tasks;
- the full audit log.

## 13. Integrity rules (non-negotiable)

1. Never invent a study, citation, statistic or quotation. Every claim in an output must trace to a stored record, page and quotation.
2. Every AI decision stores the model name and version, a hash of the prompt, the input hash, the output and a timestamp.
3. Every AI decision can be overridden by a human, and the override is logged.
4. The AI-use statement is generated automatically from the log and cannot be switched off.
5. Show a cost estimate (tokens and money) before every bulk AI run, and allow pause and resume.

## 14. Tests and acceptance

- Unit tests for each RIS dialect parser, deduplication rules, κ/PABAK, the sign test, the Wilson interval and the grading precedence.
- Gap Finder tests:
  - a synthetic corpus with known empty cells, in which every planted gap is found and no false gap is reported;
  - every gap statement traces to its supporting counts.
- A bundled demo project, with small RIS samples from Scopus and Web of Science and three open-access PDFs, that runs end to end with a mocked language model.
- **Acceptance:** a non-technical user can install the .exe, create a project, import two RIS files, screen, upload PDFs and export a PRISMA diagram and a framework figure without opening a terminal.

Start with Stage 1: the project skeleton, database schema, RIS import and deduplication, with tests. Then stop and show me.

## 15. Additional modules (roadmap)

Build these after the core sections, in the phases below. Complete each phase, with tests, before starting the next. Some modules change the data model, so put the schema hooks for **all** phases in place now (see "Schema hooks" at the end), even though the features come later.

### Phase A: integrity and collaboration (build straight after Section 14)

**A1. Reference verification.**
- Look up every reference, and every included record, in Crossref by DOI; where there is no DOI, search by title, first author and year.
- Flag mismatches in title, year or authors, records not found, and retractions or corrections (from Crossref's update information and the Retraction Watch data in Crossref).
- Block export of a manuscript reference list that contains unverified references unless the user overrides each one, with the override logged.

**A2. Citation chasing.**
- **Backward:** reference lists, from OpenAlex and Crossref, plus a parser for the reference section of uploaded PDFs.
- **Forward:** citing works, from OpenAlex and Semantic Scholar.
- Deduplicate the results against the project, screen them through the same Section 5 pipeline, and report them as "records identified through citation searching" in a separate PRISMA column.
- Respect API rate limits and send the user's email in the polite-pool header.

**A3. Active-learning screening.**
- Keep a relevance model (TF-IDF or embeddings with logistic regression) that re-ranks unscreened records after each batch of decisions.
- Show the estimated number of relevant records remaining and a stopping suggestion, using either a heuristic rule (for example, N consecutive irrelevant records) or a statistical stopping rule.
- The user chooses whether AI screens everything or only the top-ranked share, and the choice and stopping point are written into the Methods draft.

**A4. Multi-reviewer mode.**
- **Users and roles:** lead, reviewer and observer.
- **Blinding:** a reviewer cannot see other reviewers' decisions until they submit their own.
- **Conflicts:** a resolution queue, with per-pair and overall agreement (κ, PABAK) for human–human and human–AI pairs.
- **Sync:** use a shared project folder, or optional sync through a simple self-hosted server. Each reviewer keeps a local copy and merges in through an append-only decision log.

**A5. Project safety.**
- Automatic snapshots at every stage change and before every bulk AI run.
- Undo of the last bulk action.
- Optional AES-encrypted project files (the user sets the password).
- Daily backups to a folder the user chooses.

**A6. Progress and cost dashboard.**
- Records, decisions and time spent per stage.
- Tokens and money per stage and per model, with a monthly budget cap that pauses AI runs when it is reached.

**A7. Offline mode.**
- One switch routes all AI calls to a local Ollama model and disables every network call except those the user explicitly allows.
- Show a warning that local-model accuracy may be lower, and record the model used in the audit log.

### Phase B: deeper evidence synthesis

**B1. Search-string builder.**
- Drafts concept blocks and synonyms from the objectives and research questions.
- Translates the strings between Scopus, Web of Science, PubMed, EBSCO and ProQuest syntax (field tags, truncation, proximity operators).
- Checks recall: after each import, it reports which seed studies are missing and suggests terms that would find them.
- Exports the final strings to the Methods draft and to a PRISMA-S checklist.

**B2. Non-English records.**
- Detects the language of each record.
- Machine-translates titles and abstracts for screening, keeping the original text.
- Flags translated records, and allows full-text translation for extraction with a caution note.

**B3. Registration helper.**
- Maps the protocol to PROSPERO and OSF Registries fields and exports a filled form, which the user submits.
- Stores the registration number for the Methods draft.

**B4. Structured appraisal.**
- Forms for RoB 2, ROBINS-I, CASP, MMAT, JBI checklists and AXIS.
- The AI pre-fills each item with a supporting quotation and page; the human confirms.
- Traffic-light and summary bar plots (PNG, SVG, PPTX).
- Appraisal results feed the sensitivity analyses in Section 9.

**B5. Certainty of evidence.**
- GRADE for quantitative links (risk of bias, inconsistency, indirectness, imprecision, publication bias) and CERQual for qualitative findings.
- Shown alongside the vote-count grade, with a Summary of Findings table.

**B6. Table and figure extraction.**
- Detects tables in PDFs with a table-extraction library, with OCR for scanned tables.
- Extracts coefficients, standard errors, p-values, sample sizes, means and SDs into structured rows linked to the relationship and page.
- Asks the user to confirm values against a cropped image of the table.

**B7. Meta-analysis.**
- Converts reported statistics to a common effect size (r, d, Hedges' g, log odds ratio) with standard formulas, and logs each conversion.
- **Pooling:** random-effects (REML) and fixed-effect models.
- **Heterogeneity:** Q, I² and τ², with a prediction interval.
- **Plots:** forest plots, and funnel plots with Egger's test and trim-and-fill.
- **Further analyses:** subgroup analyses and meta-regression on moderators.
- Runs only where at least three independent datasets give convertible statistics for a link; otherwise it falls back to the vote count.
- Implement in Python (statsmodels or a validated meta-analysis package), and validate against published worked examples in the tests.

**B8. Qualitative synthesis.**
- A thematic-synthesis workflow: line-by-line codes from the qualitative findings, then descriptive themes, then analytical themes.
- Each theme is linked to quotations and studies, and each finding gets a CERQual rating.

**B9. Living-review updates.**
- Saves the search strings and date and re-runs the search on demand (the user imports new RIS exports).
- Deduplicates against everything already in the project and screens only new records.
- Shows changes in grades, the framework and gaps since the last version, with a version label.
- Produces an updated PRISMA diagram in the PRISMA 2020 "updated review" format.

### Phase C: outputs, writing and interoperability

**C1. Bibliometrics.**
- Publication trends, country and institution maps, keyword co-occurrence, co-authorship, and co-citation and bibliographic-coupling networks (OpenAlex data).
- Exports to VOSviewer and Gephi formats.

**C2. Interactive framework explorer.**
- An HTML page in which clicking any arrow or box shows its link ID, grade, supporting datasets, quotations and pages.
- Filters by grade, region and design.
- Exports as a single self-contained HTML file for reviewers.

**C3. Manuscript and checklists.**
- A section-by-section manuscript draft built from the project data, with journal templates for the user to choose.
- PRISMA 2020, PRISMA-S, PRISMA-ScR and ENTREQ checklists filled in with section and page locations.
- Every number in the draft is pulled from the database, never typed by the model.

**C4. Citation styles and reference managers.**
- CSL styles (APA 7, Harvard, Vancouver, and others the user loads).
- Zotero integration through its local API or a Better BibTeX export, and Mendeley through RIS/BibTeX.
- In-text citations stay linked to records.

**C5. Response-to-reviewers assistant.**
- Imports reviewer comments.
- Maps each comment to the manuscript changes, using a diff between versions, and their locations.
- Drafts responses in a point-by-point table.

**C6. Repository package.**
- One-click export of an OSF or Zenodo bundle: data, codebook, scripts, logs, figures, a README and a data dictionary.
- Full texts are excluded because of publisher licences.

**C7. Review-type templates.**
- Preset protocols, stages and outputs for systematic, scoping (PRISMA-ScR), rapid, umbrella, bibliometric and meta-analysis reviews.
- Each template switches on the relevant modules.

**C8. Interoperability.**
- Import and export of screening decisions with Rayyan, Covidence, Catchii and ASReview (CSV and RIS with decision tags).
- Coding exchange with QualCoder and other QDA software through REFI-QDA (`.qdpx`, `.qdc`), validated against the REFI-QDA schemas.

**C9. Additional review frameworks.**
- Templates for TCCM (theory, context, characteristics, methodology), ADO (antecedents, decisions, outcomes), PICO/PCC and S-O-R, besides SOBC.
- The codebook, figures and Gap Finder adapt to the chosen framework.

### Schema hooks to add now

- `users` and `roles` tables. Every decision row carries `reviewer_id` and `reviewer_type` (human or ai).
- A `source` field on records (database, citation-backward, citation-forward or update-n) and a `search_version` field.
- `appraisal_items`, `certainty_ratings`, `effect_sizes` and `qual_codes`/`themes` tables.
- `reference_checks` (DOI, status, retraction flag, checked date).
- `snapshots`, `cost_log` and `gap_register` tables.
- A `project_type` and `framework_type` on the project.

Update the tests in Section 14 so each new module ships with its own unit tests and a run through the demo project.