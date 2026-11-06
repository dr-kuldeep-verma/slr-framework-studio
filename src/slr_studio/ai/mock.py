"""Providers that answer without a network.

Two of them:

* :class:`ScriptedProvider` returns answers you hand it, in order or keyed by
  input. It is what the tests use when they need a specific disagreement.
* :class:`MockProvider` applies crude keyword rules to the record. It is what
  the bundled demo uses, so a reviewer can walk the whole pipeline before
  entering an API key, and what the end-to-end test uses.

Neither pretends to be a real screener. Both are deterministic, which is the
point: the same input always gives the same answer, so a test that fails has
found a change in the pipeline rather than in a model.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable

from .base import LLMResponse, Provider, ProviderError, Usage

__all__ = ["MockProvider", "ScriptedProvider"]


class ScriptedProvider(Provider):
    """Replays prepared answers."""

    name = "scripted"
    sends_data_off_device = False

    def __init__(
        self,
        responses: list[str] | None = None,
        *,
        by_match: list[tuple[str, str]] | None = None,
        model: str = "scripted-1",
        default: str | None = None,
    ) -> None:
        super().__init__(model)
        self.responses = list(responses or [])
        #: (regex, response) pairs tried in order against the user prompt.
        self.by_match = list(by_match or [])
        self.default = default
        self.calls: list[dict[str, Any]] = []

    def complete(self, *, system: str, user: str, max_tokens: int = 1024,
                 temperature: float = 0.0, timeout: float = 120.0) -> LLMResponse:
        self.calls.append({"system": system, "user": user})
        for pattern, response in self.by_match:
            if re.search(pattern, user, re.IGNORECASE | re.DOTALL):
                return self._respond(response, system, user)
        if self.responses:
            return self._respond(self.responses.pop(0), system, user)
        if self.default is not None:
            return self._respond(self.default, system, user)
        raise ProviderError("The scripted provider ran out of prepared answers.")

    def _respond(self, text: str, system: str, user: str) -> LLMResponse:
        return LLMResponse(
            text=text,
            model=self.model,
            model_version=self.model,
            usage=Usage(
                prompt_tokens=max(1, len(system) + len(user)) // 4,
                completion_tokens=max(1, len(text) // 4),
            ),
        )


#: Rough keyword signals the demo screener uses, mapped to exclusion codes.
#: These are not review criteria; they exist so the demo produces a plausible
#: mixture of includes, excludes and disagreements without a network.
_DEMO_RULES: tuple[tuple[str, str, str], ...] = (
    (r"\b(editorial|erratum|correction|book review)\b", "E1",
     "The record describes itself as an editorial or similar, not a research report."),
    (r"\b(scoping review|systematic review|literature review)\b", "E1",
     "The record describes itself as a review rather than a primary study."),
    (r"\b(marine biology|astronomy|semiconductor)\b", "E6",
     "The subject matter is outside the protocol's concept."),
)

#: Signals that the record is on topic. A record matching none of these and
#: none of the rules above is returned as unclear.
_DEMO_INCLUDE = (
    r"\b(participation|participatory|community|resident|co-management|"
    r"empowerment|deliberat|benefit sharing|homestay)\w*\b"
)

#: Signals that the abstract reports something a review could extract. The
#: exacting persona wants one of these before it will include.
_DEMO_OUTCOME = (
    r"\b(associat|predict|effect|relationship|outcome|increase|reduce|"
    r"correlat|explained|significant|trial|compared)\w*\b"
)


class MockProvider(Provider):
    """Answers screening and adjudication prompts from keyword rules."""

    name = "mock"
    sends_data_off_device = False

    def __init__(
        self,
        model: str = "mock-screener-1",
        *,
        persona_bias: dict[str, str] | None = None,
        hook: Callable[[str, str], str | None] | None = None,
    ) -> None:
        super().__init__(model)
        #: Lets a test make one persona stricter than the other.
        self.persona_bias = persona_bias or {}
        #: Last-resort escape hatch for a test that needs one odd answer.
        self.hook = hook
        self.n_calls = 0
        self.last_system = ""

    def complete(self, *, system: str, user: str, max_tokens: int = 1024,
                 temperature: float = 0.0, timeout: float = 120.0) -> LLMResponse:
        self.n_calls += 1
        # The mapping answer has to choose from the codebook, which arrives
        # in the system prompt; the handlers take only the user prompt, so
        # it is kept here rather than changing every handler's signature.
        self.last_system = system
        if self.hook is not None:
            override = self.hook(system, user)
            if override is not None:
                return self._wrap(override, system, user)
        payload = {
            "adjudicator": self._adjudicate,
            "qc": self._screen_strict,
            "screener_b": self._screen_strict,
            "screener_a": self._screen_inclusive,
            "assessor": self._assess_fulltext,
            "extractor": self._extract,
            "direction": self._direction,
            "codebook": self._codebook,
            "mapper": self._map_labels,
            "linkage": self._linkage,
            "contradiction": self._contradiction,
            "themes": self._themes,
            "propositions": self._propositions,
            "study": self._study,
        }[self._role(system)](user)
        return self._wrap(json.dumps(payload), system, user)

    @staticmethod
    def _role(system: str) -> str:
        """Which hat the caller is wearing, from the opening of the prompt.

        Only the first few lines are read. Every system prompt carries the
        whole protocol, and a protocol that describes its own procedure will
        contain words like "adjudicates" and "quality control" -- matching
        against the full text made this double answer as the adjudicator to
        everything.
        """
        head = system[:400].casefold()
        if "proposing a construct codebook" in head:
            return "codebook"
        if "group constructs into themes" in head:
            return "themes"
        if "draft propositions" in head:
            return "propositions"
        if "draft the study that would close one gap" in head:
            return "study"
        if "mapping labels onto a construct codebook" in head:
            return "mapper"
        if "checking whether two studies share a dataset" in head:
            return "linkage"
        if "explaining a contested finding" in head:
            return "contradiction"
        if "extracting data from a report" in head:
            return "extractor"
        if "checking one sentence" in head:
            return "direction"
        if "assessing the full text" in head:
            return "assessor"
        if "you are adjudicating" in head:
            return "adjudicator"
        if "quality-control check" in head:
            return "qc"
        if "second of two independent screeners" in head:
            return "screener_b"
        return "screener_a"

    # -- answers -----------------------------------------------------------

    def _rule_exclusion(self, text: str, user: str) -> dict[str, Any] | None:
        for pattern, code, reason in _DEMO_RULES:
            match = re.search(pattern, text)
            if match:
                return {
                    "decision": "exclude",
                    "exclusion_code": code,
                    "reason": reason,
                    "quote": self._quote_around(user, match.start()),
                }
        return None

    def _screen_inclusive(self, user: str) -> dict[str, Any]:
        """The first persona: keep anything that might be eligible."""
        text = user.casefold()
        ruled_out = self._rule_exclusion(text, user)
        if ruled_out:
            return ruled_out
        match = re.search(_DEMO_INCLUDE, text)
        if match:
            return {
                "decision": "include",
                "exclusion_code": "",
                "reason": "The record reports on resident or community participation.",
                "quote": self._quote_around(user, match.start()),
            }
        return {
            "decision": "unclear",
            "exclusion_code": "",
            "reason": "Nothing in the title or abstract settles eligibility.",
            "quote": "",
        }

    def _screen_strict(self, user: str) -> dict[str, Any]:
        """The second persona: make the record earn its place.

        It differs from the first in two ways, which is what gives the demo
        genuine disagreements to adjudicate: a record with no abstract is
        unclear rather than included, and an on-topic record that reports no
        outcome fails the usable-outcome criterion.
        """
        text = user.casefold()
        ruled_out = self._rule_exclusion(text, user)
        if ruled_out:
            return ruled_out
        if "(no abstract in this record)" in text:
            return {
                "decision": "unclear",
                "exclusion_code": "",
                "reason": "On topic, but there is no abstract to judge it from.",
                "quote": "",
            }
        match = re.search(_DEMO_INCLUDE, text)
        if not match:
            return {
                "decision": "unclear",
                "exclusion_code": "",
                "reason": "Nothing in the title or abstract settles eligibility.",
                "quote": "",
            }
        if not re.search(_DEMO_OUTCOME, text):
            return {
                "decision": "exclude",
                "exclusion_code": "E7",
                "reason": "On topic, but the abstract reports no usable outcome.",
                "quote": self._quote_around(user, match.start()),
            }
        return {
            "decision": "include",
            "exclusion_code": "",
            "reason": "On topic and the abstract reports an outcome.",
            "quote": self._quote_around(user, match.start()),
        }

    #: Phrases in a full text that mean it is not a primary study.
    _FULLTEXT_RULES: tuple[tuple[str, str, str], ...] = (
        ("does not report primary data", "E1",
         "The report states that it presents no primary data."),
        ("reports a scoping review", "E1",
         "The report describes itself as a review of existing studies."),
        ("collects no new data", "E1",
         "The report collects no new data of its own."),
    )

    @staticmethod
    def _pdf_pages(user: str) -> list[tuple[int, str]]:
        """Split the rendered report back into its labelled pages."""
        pages: list[tuple[int, str]] = []
        pattern = re.compile(r"\[PDF page (\d+)[^\]]*\]\n")
        matches = list(pattern.finditer(user))
        for index, match in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(user)
            pages.append((int(match.group(1)), user[match.end():end].strip()))
        return pages

    def _assess_fulltext(self, user: str) -> dict[str, Any]:
        """Decide a report, quoting words that really are on the page cited.

        Quoting accurately is the whole point of the stage this double
        stands in for, so it copies a run of words verbatim out of one page
        and reports that page's number.
        """
        pages = self._pdf_pages(user)
        if not pages:
            return {
                "decision": "unclear",
                "exclusion_code": "",
                "reason": "No readable text was supplied for this report.",
                "quote": "",
                "quote_page": None,
            }

        for phrase, code, reason in self._FULLTEXT_RULES:
            for page_no, text in pages:
                index = text.casefold().find(phrase)
                if index == -1:
                    continue
                return {
                    "decision": "exclude",
                    "exclusion_code": code,
                    "reason": reason,
                    "quote": " ".join(text[index:index + 140].split()[:14]),
                    "quote_page": page_no,
                }

        # Otherwise include, quoting from the first page with enough words.
        for page_no, text in pages:
            words = text.split()
            if len(words) >= 20:
                return {
                    "decision": "include",
                    "exclusion_code": "",
                    "reason": (
                        "The report presents primary data on resident or "
                        "community participation and is eligible."
                    ),
                    "quote": " ".join(words[8:20]),
                    "quote_page": page_no,
                }
        return {
            "decision": "unclear",
            "exclusion_code": "",
            "reason": "Too little text could be read to decide.",
            "quote": "",
            "quote_page": None,
        }

    #: Sentences the offline extractor can turn into a relationship. Each is
    #: a regular expression over a page's text; the match groups give the two
    #: labels and the words in between give the direction.
    _RELATIONSHIP_RE = re.compile(
        r"([A-Za-z][A-Za-z \-]{3,60}?)\s+"
        # The auxiliary is optional: "fairness explained ..." states a
        # relationship just as "fairness was associated with ..." does.
        r"(?:(?:was|were|is|are)\s+)?"
        r"(strongly |weakly |positively |negatively |)"
        r"(?:associated with|related to|predicted"
        r"|explained(?:\s+[\d.]+%\s+of\s+the\s+variance\s+in)?)\s+"
        r"([a-z][A-Za-z \-]{3,60}?)"
        r"(?=[.,(]|\s+after|\s+when|\s+and\b)",
    )

    _DESIGN_RULES: tuple[tuple[str, str, str], ...] = (
        ("cluster randomised", "randomised_experiment", "RoB 2"),
        ("randomised controlled", "randomised_experiment", "RoB 2"),
        ("cross-sectional", "cross_sectional", "AXIS"),
        ("two-wave panel", "longitudinal", "AXIS"),
        ("longitudinal", "longitudinal", "AXIS"),
        ("semi-structured interview", "qualitative", "CASP"),
        ("qualitative", "qualitative", "CASP"),
        ("mixed-methods", "mixed_methods", "MMAT"),
        ("scoping review", "review", "CASP"),
    )

    def _extract(self, user: str) -> dict[str, Any]:
        """Pull a study description and relationships out of the report text.

        Crude pattern matching, not reading. It exists so the extraction
        pipeline — quotation checks, the design rule, the direction check —
        can be exercised end to end without a network, on text that really is
        in the report.
        """
        pages = self._pdf_pages(user)
        whole = " ".join(text for _, text in pages)
        lowered = whole.casefold()

        design, family, instrument = "", "unclear", "none"
        for phrase, fam, tool in self._DESIGN_RULES:
            if phrase in lowered:
                design, family, instrument = phrase, fam, tool
                break

        manipulated = any(
            phrase in lowered for phrase in ("assigned half", "randomly assigned",
                                             "manipulated")
        )
        control = any(
            phrase in lowered for phrase in ("control group", "usual practice",
                                             "comparison group")
        )
        sample = re.search(r"\b([\d,]{2,7})\s+(households|residents|adults|participants)",
                           whole)
        country = next(
            (name for name in ("Kerala", "India", "Norway", "Spain", "Nigeria")
             if name.casefold() in lowered),
            "",
        )

        relationships: list[dict[str, Any]] = []
        for page_no, text in pages:
            for match in self._RELATIONSHIP_RE.finditer(text):
                predictor = " ".join(match.group(1).split())
                outcome = " ".join(match.group(3).split())
                qualifier = match.group(2).strip().casefold()
                direction = "negative" if qualifier == "negatively" else "positive"
                statistic = ""
                nearby = text[match.end(): match.end() + 120]
                stat = re.search(r"\(?\br\s*=\s*-?\d?\.\d+|\b\d{1,3}%", nearby)
                if stat:
                    statistic = stat.group(0).strip("( ")
                quote = " ".join(text[match.start():match.end()].split())
                relationships.append({
                    "predictor_label": predictor,
                    "outcome_label": outcome,
                    "relationship_type": "association",
                    "direction": direction,
                    "strength": "",
                    "statistic": statistic,
                    "significance": "significant" if statistic else "not_reported",
                    "evidence_basis": "statistical" if statistic else "thematic",
                    "quote": quote,
                    "quote_page": page_no,
                })

        first_page = pages[0][1] if pages else ""
        report_quote = " ".join(first_page.split()[:14])
        return {
            "report": {
                "country": country,
                "setting": "",
                "population": sample.group(2) if sample else "",
                "design": design,
                "design_family": family,
                "manipulated_treatment": manipulated,
                "control_group": control,
                "design_note": "",
                "sampling": "",
                "sample_size": sample.group(1) if sample else None,
                "sample_size_text": sample.group(0) if sample else "",
                "time_horizon": "",
                "theories": [],
                "framework_stages": [],
                "key_findings": relationships[0]["quote"] if relationships else "",
                "limitations": "",
                "appraisal_instrument": instrument,
                "appraisal_note": "Read by the offline extractor, not appraised.",
                "appraisal_concern": "unclear",
                "quote": report_quote,
                "quote_page": pages[0][0] if pages else None,
            },
            "relationships": relationships,
        }

    @staticmethod
    def _direction(user: str) -> dict[str, Any]:
        """Re-read the direction out of the quotation alone."""
        match = re.search(r'Quotation from the report:\s*"(.*?)"', user, re.DOTALL)
        quote = (match.group(1) if match else user).casefold()
        if re.search(r"\bnegatively\b|\breduced?\b|\bless\b|\binverse\b", quote):
            return {"direction": "negative", "note": "the quotation says it reduces it"}
        if re.search(r"\bno (significant )?(association|relationship|effect)\b", quote):
            return {"direction": "none", "note": "the quotation denies a relationship"}
        if re.search(r"\bassociated with\b|\bpredicted\b|\brelated to\b|\bexplained\b"
                     r"|\bhigher\b|\bincreas", quote):
            return {"direction": "positive", "note": "the quotation says more goes with more"}
        return {"direction": "unclear", "note": "the quotation states no direction"}

    def _adjudicate(self, user: str) -> dict[str, Any]:
        # The demo adjudicator sides with inclusion, which is the conservative
        # choice at title and abstract: a wrongly included record is caught at
        # full text, a wrongly excluded one is never seen again.
        return {
            "decision": "include",
            "exclusion_code": "",
            "reason": (
                "The screeners disagreed and nothing in the record rules it "
                "out, so it is carried forward to full text."
            ),
            "quote": "",
            "sided_with": "screener_a",
        }

    @staticmethod
    def _quote_around(user: str, position: int, n_words: int = 12) -> str:
        """A quotation taken only from the record's own title or abstract.

        The prompt also carries metadata lines, and a window cut blindly
        across them produces a string that appears nowhere in the record --
        which the engine's quotation check then flags as fabricated, quite
        correctly. A test double should exercise that check, not trip it.
        """
        marker = "Abstract:\n"
        index = user.find(marker)
        if index != -1:
            source = user[index + len(marker):].strip()
            if source.startswith("(no abstract"):
                source = ""
        else:
            source = ""
        if not source:
            for line in user.splitlines():
                if line.startswith("Title: "):
                    source = line[len("Title: "):]
                    break
        words = source.split()
        if not words:
            return ""
        # Fifteen words is the ceiling the prompt sets; stay inside it.
        return " ".join(words[:n_words])

    # -- synthesis ---------------------------------------------------------
    #
    # The synthesis answers are built by grouping labels on their words. That
    # is exactly what the real prompt forbids -- mapping must go by what was
    # measured, not by vocabulary -- and the difference does not matter here:
    # what these exercise is the pipeline around the answer (approval gates,
    # orientation, sign flips, dataset votes, grading), not the judgement
    # inside it. A test that needs a particular judgement uses
    # ScriptedProvider instead.

    #: Stop words that carry no topic, so they never become a construct.
    _STOP = frozenset(
        "the a an of in on for and or to with by from at is are per level "
        "perceived overall total general local".split()
    )

    def _label_lines(self, user: str) -> list[tuple[str, str]]:
        """(label, role) pairs out of a prompt that lists them."""
        found = []
        for line in user.splitlines():
            match = re.match(r"- (.+?) \((?:used as )?(\w+)[,)]", line.strip())
            if match:
                found.append((match.group(1).strip(), match.group(2)))
        return found

    def _head_word(self, label: str) -> str:
        words = [
            w for w in re.sub(r"[^a-z ]", " ", label.casefold()).split()
            if w not in self._STOP and len(w) > 2
        ]
        return words[-1] if words else (label.casefold() or "unnamed")

    #: How many constructs the offline double puts in one theme. Small
    #: enough that the demonstration produces more than one theme.
    _THEME_SIZE = 3

    def _themes(self, user: str) -> dict[str, Any]:
        """Group the constructs in the order they were given.

        No cleverness: the point of the double is that the pipeline runs
        offline, not that it groups well. Grouping by nothing but order is
        also visibly arbitrary, which is the honest signal to a reader of the
        demonstration project that no reviewer chose these themes.
        """
        names = [
            line.split("—")[0].strip(" -\t")
            for line in user.splitlines()
            if line.startswith("- ")
        ]
        names = [name[2:].strip() if name.startswith("- ") else name
                 for name in names]
        names = [name for name in names if name]
        themes = []
        for index in range(0, len(names), self._THEME_SIZE):
            group = names[index : index + self._THEME_SIZE]
            themes.append(
                {
                    "name": f"Theme {index // self._THEME_SIZE + 1}: "
                            f"{group[0]}",
                    "description": (
                        "Grouped by the order the constructs were listed, "
                        "which is what an offline double can do. No reviewer "
                        "chose this grouping."
                    ),
                    "constructs": group,
                }
            )
        return {"themes": themes}

    #: The inventory lines the proposition prompt shows, e.g.
    #: "- L3: trust → participation | positive | strong | 4 dataset(s), ...".
    _INVENTORY_LINE = re.compile(
        r"^- L(?P<id>\d+): (?P<predictor>.+?) \u2192 (?P<outcome>.+?) \| "
        r"(?P<direction>\w+) \| (?P<grade>\w+)",
        re.MULTILINE,
    )

    def _propositions(self, user: str) -> dict[str, Any]:
        """One proposition per graded link, citing that link and no other.

        Citing exactly what it was shown is the only behaviour that lets the
        demonstration exercise the grounding check without tripping it, and
        it is what a careful model would do.
        """
        propositions = []
        for match in self._INVENTORY_LINE.finditer(user):
            predictor = match.group("predictor").strip()
            outcome = match.group("outcome").strip()
            direction = match.group("direction")
            verb = {
                "positive": "is associated with more",
                "negative": "is associated with less",
            }.get(direction, "is associated with")
            propositions.append(
                {
                    "statement": f"Higher {predictor} {verb} {outcome}.",
                    "mechanism": (
                        f"Reported as a {match.group('grade')} link; the "
                        f"mechanism is not established by an offline double "
                        f"and must be written by the reviewer."
                    ),
                    "theories": [],
                    "moderators": "not established offline",
                    "boundary_conditions": (
                        "Holds only for the settings the included studies "
                        "covered."
                    ),
                    "falsifying_test": (
                        f"A study in the same setting finding no relationship "
                        f"between {predictor} and {outcome}."
                    ),
                    "element_kind": "evidenced_link",
                    "cited_links": [f"L{match.group('id')}"],
                }
            )
        return {"propositions": propositions[:12]}

    def _study(self, user: str) -> dict[str, Any]:
        """A study drafted from the gap's own words.

        The double restates the gap rather than inventing a design, which is
        the honest thing for a keyword rule to do and keeps the draft
        traceable to the statement it came from.
        """
        statement = ""
        for line in user.splitlines():
            line = line.strip()
            if line and not line.startswith(("The gap", "The counts", "Priority", "{", "}", '"')):
                statement = line
                break
        subject = statement.rstrip(".") or "the gap this review identified"
        population = "the populations the included studies did not cover"
        for marker in ("in low", "in high", "in upper", "in lower"):
            if marker in statement.casefold():
                population = statement[statement.casefold().index(marker):][:80]
                break
        return {
            "question": (
                f"What would be found if a study addressed this gap: {subject}?"
            ),
            "hypothesis": (
                "A study addressing this gap would find a relationship in the "
                "direction the review's propositions predict."
            ),
            "design": (
                "A design matched to the gap; an offline double cannot choose "
                "one and the reviewer must."
            ),
            "measures": ["the measures the gap names"],
            "population": population,
            "setting": "the setting the gap names",
            "proposition": "",
        }

    def _codebook(self, user: str) -> dict[str, Any]:
        groups: dict[str, list[str]] = {}
        for label, _role in self._label_lines(user):
            groups.setdefault(self._head_word(label), []).append(label)
        constructs = []
        for head, labels in sorted(groups.items())[:25]:
            name = head.replace("_", " ")
            constructs.append(
                {
                    "name": name,
                    "definition": (
                        f"What the studies measured when they wrote about "
                        f"{name}."
                    ),
                    "includes": sorted(set(labels))[:4],
                    "excludes": [f"anything not concerning {name}"],
                    "example_labels": sorted(set(labels))[:3],
                }
            )
        if not constructs:
            constructs = [
                {
                    "name": "unnamed construct",
                    "definition": "No labels were supplied to group.",
                    "includes": ["nothing"],
                    "excludes": ["everything"],
                }
            ]
        return {"constructs": constructs, "note": "grouped by head word"}

    def _map_labels(self, user: str) -> dict[str, Any]:
        available = {
            line[4:].strip().casefold()
            for line in self.last_system.splitlines()
            if line.startswith("### ")
        } if self.last_system else set()
        mappings = []
        for label, role in self._label_lines(user):
            head = self._head_word(label).replace("_", " ")
            target = head if head in available else "other"
            mappings.append(
                {
                    "label": label,
                    "construct": target,
                    "measured_as": f"whatever the authors meant by {label!r}",
                    "rationale": f"the label turns on {head!r}",
                    "confidence": 0.8 if target != "other" else 0.4,
                    "contestable": target == "other",
                    "contest_reason": (
                        "no construct in the codebook covers this label"
                        if target == "other"
                        else ""
                    ),
                }
            )
        return {"mappings": mappings}

    def _linkage(self, user: str) -> dict[str, Any]:
        """Same sample size and country is taken as the same fieldwork."""
        sizes = re.findall(r"Sample size: (\d+)", user)
        countries = re.findall(r"Country: (.+)", user)
        same_size = len(sizes) == 2 and sizes[0] == sizes[1]
        same_country = len(countries) == 2 and countries[0].strip() == countries[1].strip()
        if same_size and same_country:
            return {
                "same_dataset": "yes",
                "confidence": 0.8,
                "reason": "the same sample size in the same country",
                "shared_signals": ["sample_size", "country"],
            }
        return {
            "same_dataset": "no",
            "confidence": 0.7,
            "reason": "the studies report different samples",
            "shared_signals": [],
        }

    def _contradiction(self, user: str) -> dict[str, Any]:
        countries = set(re.findall(r"- (\w[\w ]*?);", user))
        if len(countries) > 1:
            return {
                "explanation_type": "context",
                "explanation": (
                    "The datasets come from different countries, and the "
                    "relationship may not hold the same way in each."
                ),
                "suggestion_type": "moderator",
                "suggested_moderator": "country context",
                "split_into": [],
            }
        return {
            "explanation_type": "unclear",
            "explanation": (
                "The study characteristics shown do not explain why these "
                "datasets disagree."
            ),
            "suggestion_type": "none",
            "suggested_moderator": "",
            "split_into": [],
        }

    def _wrap(self, text: str, system: str, user: str) -> LLMResponse:
        return LLMResponse(
            text=text,
            model=self.model,
            model_version=self.model,
            usage=Usage(
                prompt_tokens=max(1, (len(system) + len(user)) // 4),
                completion_tokens=max(1, len(text) // 4),
            ),
        )
