"""Paper-level retrieval over historical conference reviews.

The index deliberately keeps one vector per paper.  Similar papers are found
from title + abstract; only the matched papers' redacted qualitative review
fields are decompressed afterwards. Historical reviews are reviewer-concern memory,
not evidence about the manuscript currently under review.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import importlib
import json
import math
import os
import re
import shlex
import shutil
import struct
import tempfile
import time
import uuid
import zlib
from collections.abc import Awaitable, Callable, Iterable
from pathlib import Path
from typing import Any, Self

INDEX_SCHEMA_VERSION = "paper_review_memory_faiss_v1"
INDEX_OWNER = "omniscientist.paper-review.review-memory"
EMBEDDING_TEXT_POLICY = "specter2_title_sep_abstract_v2"
REVIEW_TEXT_POLICY = "author_guidance_redacted_v6"
DEFAULT_TOP_K = 5
MAX_REVIEW_JSON_BYTES = 16 * 1024 * 1024
MAX_REVIEW_PACKET_BYTES = 16 * 1024 * 1024
_SKILL_DIR = Path(__file__).resolve().parent
_INDEX_HEADER = "index.json"
_GENERATIONS_DIR = "generations"
_VECTOR_FILE = "vectors.faiss"
_PAPERS_FILE = "papers.jsonl"
_REVIEWS_FILE = "reviews.pack"
_GENERATION_RE = re.compile(r"gen-[0-9a-f]{32}\Z")
_INDEX_FORMAT = "faiss-idmap2-flat-ip"

_SPACE_RE = re.compile(r"\s+")
_AUTHOR_GUIDANCE_FIELDS = {
    "details_of_ethics_concerns",
    "questions",
    "strengths",
    "summary",
    "weaknesses",
}
_HISTORICAL_SCORE_OR_DECISION_RE = re.compile(
    r"(?i)(?:"
    r"\bmy\s+(?:(?:low|lower|high|higher|original|confidence)\s+)?"
    r"(?:(?:initial|current|overall|final|review|evaluation)\s+)?"
    r"(?:scores?|ratings?|recommendation|vote)\b|"
    r"\bmy\s+(?:relatively\s+|rather\s+)?(?:low|lower|lowest|high|higher)\s+"
    r"confidence\s+(?:scores?|ratings?|levels?)\b|"
    r"\bmy\s+(?:actual|true)\s+(?:score|rating)\b|"
    r"\bmy\s+choice\s+of\s+rating\b|"
    r"\b(?:the|a)\s+(?:score|rating)\s+i\s+(?:gave|assigned|picked)\b|"
    r"\b(?:my|reviewer|review)\s+recommendation\s+score\b|"
    r"\b(?:this|the)\s+review\s+(?:score|rating|recommendation|vote)\b|"
    r"\b(?:raise|increase|lower|decrease|change|update|upgrade)\b"
    r"[^.!?\n]{0,80}\b(?:my\s+|(?:this|the)\s+review\s+)"
    r"(?:score|rating|recommendation|vote)\b|"
    r"\bmy\s+(?:score|rating|recommendation|vote)\b"
    r"[^.!?\n]{0,80}\b(?:raise|increase|lower|decrease|change|update|upgrade)\b|"
    r"\bi\b[^.!?\n]{0,40}\b(?:raise|increase|lower|decrease|change|update|upgrade)"
    r"\b[^.!?\n]{0,40}\bthe\s+(?:score|rating|recommendation|vote)\b|"
    r"\b(?:strong|weak|borderline)\s+(?:accept|reject)\b|"
    r"\b(?:lean|leans|leaning)\s+towards?\s+(?:a\s+)?"
    r"(?:(?:strong|weak|borderline)\s+)?"
    r"(?:acceptance|rejection|accept|reject)\b|"
    r"\blean\s+(?:accept|reject)\b|"
    r"\bmy\s+(?:assessment|judgment|evaluation)\b[^.!?\n]{0,60}\b"
    r"(?:toward|towards|for)\s+(?:acceptance|rejection)\b|"
    r"\bclear\s+(?:accept|reject)[-\s]level\s+paper\b|"
    r"\bconstitute(?:s|d)?\s+(?:an?\s+)?clear\s+(?:accept|reject)\b|"
    r"\breason\s+for\s+(?:(?:a|the)\s+)?(?:strong\s+)?rejection\b|"
    r"\bmove(?:s|d|ing)?\b[^.!?\n]{0,50}\b(?:toward|towards|into)\s+"
    r"(?:acceptance|rejection|the\s+accept\s+range|the\s+reject\s+range)\b|"
    r"^\s*(?:[-*+]\s+)?(?:\*\*)?(?:(?:lean|strong|weak|borderline)\s+)?"
    r"(?:accept|reject)(?:ance|ion)?(?:\*\*)?[.!]?\s*$|"
    r"\b(?:initial|overall|final|current)\s+recommendation\b|"
    r"\blean(?:ing)?(?:\s+towards?)?\s+(?:a\s+)?"
    r"(?:positive|negative)\s+recommendation\b|"
    r"\bmy\s+(?:publication|positive|negative|submission)?\s*recommendation\b"
    r"(?!\s+(?:to|for)\s+(?:the\s+)?authors?\s+"
    r"(?:(?:is|would\s+be)\s+)?to\b)|"
    r"\b(?:keep|maintain|retaining?)\b[^.!?\n]{0,30}\b"
    r"(?:positive|negative)\s+recommendation\b|"
    r"(?<!\()\bi\b(?!\))[^.!?\n]{0,180}\b(?:maintain|keep)\b"
    r"[^.!?\n]{0,30}\b(?:an?\s+|the\s+|my\s+|this\s+)?"
    r"(?:(?:positive|negative|low|lower|high|higher|original|current|"
    r"slightly\s+skeptical)\s+)?(?:score|rating)s?\b"
    r"(?!\s+(?:normalization|function|method|term|metric|threshold|head|model))|"
    r"\bthe\s+reviewer\b[^.!?\n]{0,180}\b(?:maintain|keep)\b"
    r"[^.!?\n]{0,30}\b(?:an?\s+|the\s+|my\s+|this\s+)?"
    r"(?:(?:positive|negative|low|lower|high|higher|original|current|"
    r"slightly\s+skeptical)\s+)?(?:score|rating)s?\b|"
    r"\b(?:maintain\s+this|keep\s+the)\s+(?:score|rating)\b"
    r"(?!\s+(?:normalization|function|method|term|metric|threshold|head|model))|"
    r"(?<!\()\bi\b(?!\))[^.!?\n]{0,100}\b"
    r"(?:raise|raising|increase|increasing|improve|improving|lower|lowering|"
    r"change|changing|update|updating|adjust|adjusting|assign|assigning|"
    r"revise|revising|reconsider|reconsidering|up[-\s]?level(?:ing)?|"
    r"consider\s+raising)\b\s+(?:an?\s+|the\s+|my\s+|this\s+)?"
    r"(?:(?:the\s+)?paper['’]s\s+|review\s+|recommendation\s+)?"
    r"(?:scores?|ratings?)\b|"
    r"\bthe\s+reviewer\b[^.!?\n]{0,100}\b"
    r"(?:raise|raising|increase|increasing|improve|improving|lower|lowering|"
    r"change|changing|update|updating|adjust|adjusting|assign|assigning|"
    r"revise|revising|reconsider|reconsidering|up[-\s]?level(?:ing)?|"
    r"consider\s+raising)\b\s+(?:an?\s+|the\s+|my\s+|this\s+)?"
    r"(?:(?:the\s+)?paper['’]s\s+|review\s+|recommendation\s+)?"
    r"(?:scores?|ratings?)\b|"
    r"\b(?:improve|revise|revising|up[-\s]?level(?:ing)?|reconsider(?:ing)?)\b"
    r"\s+(?:the\s+|this\s+|(?:this|the)\s+paper['’]s\s+)?ratings?\b|"
    r"\b(?:score|rating)\b[^.!?\n]{0,50}\b(?:raise|rise|increase)\b"
    r"[^.!?\n]{0,30}\bto\s+(?:an?\s+)?\d+(?:\.\d+)?\b|"
    r"\b(?:justification|rationale)\s+for\s+(?:the\s+)?(?:score|rating)\b|"
    r"\b(?:set|lower|raise|adjust)\s+my\s+confidence\s+level\b|"
    r"\b(?:put|give)\s+(?:an?\s+)?\d+(?:\.\d+)?\s+for\s+my\s+confidence\b|"
    r"\b(?:neutral|positive|negative)\s+review\b[^.!?\n]{0,80}\b"
    r"confidence\b|"
    r"\bcurrently\s+rating\s+(?:it|this\s+paper)\s+"
    r"\d+(?:\.\d+)?(?:\s*/\s*\d+)?\b|"
    r"\b(?:this|the)\s+(?:paper|manuscript|submission|work)\s+"
    r"(?:should|must|would)\s+be\s+(?:accepted|rejected)\b|"
    r"\b(?:i|we)\s+(?:(?:would|will|strongly|therefore)\s+)?"
    r"(?:accept|reject)\s+(?:this|the)\s+"
    r"(?:paper|manuscript|submission|work)\b|"
    r"\b(?:i|we)\b[^.!?\n]{0,100}\b"
    r"(?:recommend(?:ed|ing)?|vote|voting|lean|leaning|inclined|"
    r"argue|arguing|favou?r|cast(?:ing)?)\b[^.!?\n]{0,100}\b"
    r"(?:accept(?:ance|ed|ing)?|reject(?:ion|ed|ing)?)"
    r"(?!\s+(?:sampling|sampler|algorithm|method|criterion|region))\b|"
    r"\b(?:recommend(?:ed|ing)?|vote|voting)\b[^.!?\n]{0,80}\b"
    r"(?:accept(?:ance|ed|ing)?|reject(?:ion|ed|ing)?)"
    r"(?!\s+(?:sampling|sampler|algorithm|method|criterion|region))\b|"
    r"\b(?:change|update)\s+my\s+review\b[^.!?\n]{0,80}\b"
    r"(?:accept|reject)\b|"
    r"\b(?:above|below|at)\s+(?:(?:the|an?)\s+)?"
    r"(?:[A-Za-z0-9-]+['’]?s?\s+)?"
    r"acceptance\s+(?:threshold|bar)\b|"
    r"\b(?:warrant|warrants|merit|merits|deserve|deserves)\b"
    r"[^.!?\n]{0,50}\b(?:acceptance|rejection)\b|"
    r"\b(?:not\s+)?ready\s+for\s+(?:acceptance|publication)\b|"
    r"\b(?:not\s+)?good\s+enough\s+for\s+publication\b|"
    r"\bunacceptable\s+for\s+publication\b|"
    r"\brequires?\s+major\s+revisions?\b[^.!?\n]{0,60}\b"
    r"(?:accepted|published)\s+(?:at|to|in)\s+(?:this|the)\s+conference\b|"
    r"\bbefore\b[^.!?\n]{0,80}\b(?:can|could|should|would)\s+be\s+"
    r"(?:accepted|published)\b|"
    r"\b(?:my\s+)?(?:main\s+)?reason(?:s)?\s+(?:for|behind)\s+"
    r"(?:acceptance|rejection)\b|"
    r"\b(?:worthy|deserving)\s+of\s+(?:acceptance|publication)\b|"
    r"\b(?:polished|mature|complete|strong|good)\s+enough\s+to\s+be\s+publishable\b|"
    r"\bin\s+(?:a\s+)?publishable\s+(?:state|form|condition)\b|"
    r"\bin\s+(?:an?\s+)?unpublishable\s+(?:state|form|condition)\b|"
    r"\b(?:not|hardly)\s+publishable\b|"
    r"\b(?:not\s+)?(?:yet\s+)?suitable\s+for\s+publication\b|"
    r"\bnot\s+(?:yet\s+)?at\s+(?:a\s+)?publishable\s+standard\b|"
    r"\b(?:deserve|deserves|deserving)\s+(?:a\s+)?publication\b|"
    r"\blacks?\s+(?:a\s+)?publishable\s+"
    r"(?:contribution|result|state|quality)\b|"
    r"\brender(?:s|ed|ing)?\b[^.!?\n]{0,40}\b"
    r"(?:it|paper|manuscript|submission|work)\b[^.!?\n]{0,20}\bpublishable\b|"
    r"\brender(?:s|ed|ing)?\b[^.!?\n]{0,60}\bunpublishable\b|"
    r"\b(?:meet|meets|meeting|satisfy|satisfies|satisfying|fulfill|fulfills|fulfilling)\b"
    r"[^.!?\n]{0,60}\b(?:acceptance|publication)\s+"
    r"(?:criteria|bar|standard|threshold)\b|"
    r"\bfor\s+the\s+purpose\s+of\s+acceptance\b|"
    r"\b(?:not\s+)?ready\s+for\s+(?:this|the)\s+(?:venue|conference)\b|"
    r"\b(?:sufficient|enough)\s+(?:novelty|quality|contribution|evidence|significance)"
    r"\s+for\s+(?:a\s+)?publication(?:\s+at\s+(?:this|the)\s+venue)?\b|"
    r"\bi(?:['’]d|\s+would|\s+currently)?\b[^.!?\n]{0,30}\brate\s+"
    r"(?:it|(?:this|the)\s+(?:paper|submission|work))\s+"
    r"(?:(?:as|a|at)\s+)?\d+(?:\.\d+)?"
    r"(?:\s+or\s+\d+(?:\.\d+)?)?\b|"
    r"\bi\s+(?:give|gave|assign|assigned)\s+"
    r"(?:(?:it|(?:this|the)\s+(?:paper|submission|work))\s+)?"
    r"(?:a\s+)?\d+(?:\.\d+)?\s+instead\s+of\s+(?:a\s+)?"
    r"\d+(?:\.\d+)?\b|"
    r"\bi\s+give\s+\d+(?:\.\d+)?\s+points?\s+in\s+"
    r"(?:presentation|soundness|contribution|confidence)\b|"
    r"\boverall\s+(?:score|rating)\s*[:=]\s*\d+(?:\.\d+)?"
    r"(?:\s*/\s*\d+)?\b|"
    r"\b(?:(?:reason|justification)\s+for\s+(?:the\s+)?final\s+score|"
    r"reconsider\s+(?:my|the)?\s*rating)\b|"
    r"\breason\s+for\s+(?:giving|assigning)\b[^.!?\n]{0,30}\b"
    r"(?:score|rating)\b|"
    r"\b(?:lead|leads|leading|move|moves|moving)\b[^.!?\n]{0,50}\b"
    r"(?:higher|lower)\s+(?:score|rating)\b|"
    r"\b(?:result|results|resulting|contribute|contributes|contributed|contributing|"
    r"prevent|prevents|prevented|preventing|justify|justifies|justified|justifying|"
    r"provide|provides|provided|providing|consider|considers|considered|considering|"
    r"start|starts|started|starting)\b[^.!?\n]{0,70}\b"
    r"(?:higher|lower)\s+(?:score|rating)\b|"
    r"\b(?:happy|willing|prepared)\b[^.!?\n]{0,40}\b"
    r"(?:raise|increase|lower|decrease|change|update|upgrade)\b"
    r"[^.!?\n]{0,40}\b(?:score|rating)s?\b|"
    r"\b(?:raise|raising|increase|increasing|lower|lowering)\b"
    r"[^.!?\n]{0,30}\bto\s+(?:an?\s+)?(?:score|rating)\s+of\s+"
    r"\d+(?:\.\d+)?(?:\s*/\s*\d+)?\b|"
    r"\b(?:give|giving|assign|assigning)\s+(?:a\s+)?"
    r"(?:higher|lower)\s+(?:score|rating)\b|"
    r"\b(?:(?:primary|main)\s+reason|reason\s+(?:for|I|why))\b"
    r"[^.!?\n]{0,80}\b"
    r"(?:score|rating|rate)\b|"
    r"\bassign(?:ed|ing)?\s+(?:an?\s+)?rate\s+of\s+\d+(?:\.\d+)?\b|"
    r"\b(?:reject|accept)\s+score\b|"
    r"\b(?:not|more|less)\s+(?:yet\s+)?"
    r"(?:ready|good\s+enough|suitable|appropriate)\b[^.!?\n]{0,100}\b"
    r"(?:publication|submission|venues?|conferences?|journals?|workshops?|tracks?|"
    r"ICLR|NeurIPS|ICML|ACL|ARR|AAAI|CVPR)\b|"
    r"\b(?:better|best|more|less)\s+(?:a\s+|the\s+)?fit\b"
    r"[^.!?\n]{0,80}\b(?:venues?|conferences?|journals?|workshops?|tracks?)\b|"
    r"\b(?:better|best|more\s+appropriately)\s+submitted\b"
    r"[^.!?\n]{0,60}\b(?:venues?|conferences?|journals?|workshops?|tracks?)\b|"
    r"\b(?:not|barely|borderline)\s+(?:nearly\s+)?strong\s+enough\b"
    r"[^.!?\n]{0,80}\b(?:ICLR|NeurIPS|ICML|ACL|ARR|AAAI|CVPR|"
    r"paper|venue|conference|track)\b|"
    r"\brecommend(?:ed|ing)?\b[^.!?\n]{0,180}\b"
    r"(?:submit|submitted|submitting|submission)\b[^.!?\n]{0,180}\b"
    r"(?:venues?|conferences?|journals?|tracks?)\b|"
    r"\b(?:i|we)\b[^.!?\n]{0,100}\b"
    r"(?:suggest|encourage|recommend|worry|curious)\b[^.!?\n]{0,180}\b"
    r"(?:submit|submitted|submitting|submission|presented)\b[^.!?\n]{0,180}\b"
    r"(?:venues?|conferences?|journals?|workshops?|tracks?|"
    r"ICLR|NeurIPS|ICML|ACL|ARR|AAAI|CVPR)\b|"
    r"\b(?:should|could|may|might|perhaps)\b[^.!?\n]{0,100}\b"
    r"(?:submit|submitted|submitting|presented)\b[^.!?\n]{0,140}\b"
    r"(?:venues?|conferences?|journals?|workshops?|tracks?|paper)\b|"
    r"\b(?:this|the)\s+paper\s+should\s+be\s+reviewed\b"
    r"[^.!?\n]{0,80}\b(?:venues?|conferences?|journals?|workshops?|tracks?)\b|"
    r"\b(?:or\s+)?better\b[^.!?\n]{0,40}\b"
    r"(?:submit|submitting)\b[^.!?\n]{0,100}\b"
    r"(?:venues?|conferences?|journals?|workshops?|tracks?)\b|"
    r"\b(?:this|the|current)\s+(?:paper|work|manuscript|version)\b"
    r"[^.!?\n]{0,160}\bsubmitted\b[^.!?\n]{0,160}\b"
    r"(?:fitting|fit|read|venue|conference|journal|workshop|track)\b|"
    r"\bdecision\s+to\s+submit\b[^.!?\n]{0,160}\b"
    r"(?:venues?|conferences?|journals?|workshops?|tracks?|"
    r"ICLR|NeurIPS|ICML|ACL|ARR|AAAI|CVPR)\b|"
    r"\b(?:better|more)\s+(?:fitting|suited|suitable|appropriate)\b"
    r"[^.!?\n]{0,100}\b(?:venues?|conferences?|journals?|workshops?|tracks?)\b|"
    r"\bpresented\s+as\s+workshop\s+contributions?\b|"
    r"\brecommend(?:ed|ing)?\b[^.!?\n]{0,100}\b"
    r"(?:venue|conference|journal|track)\b[^.!?\n]{0,60}\bsubmission\b|"
    r"\b(?:ICLR|NeurIPS|ICML|ACL|ARR|AAAI|CVPR)\b[^.!?\n]{0,60}\b"
    r"(?:not\s+|isn't\s+|is\s+not\s+)?(?:the\s+)?right\s+venue\b|"
    r"\bwrong\s+venue\b|"
    r"\bright\s+venue\b|"
    r"\b(?:suitable|suited|ready|fit|appropriate|good\s+enough)\b"
    r"[^.!?\n]{0,80}\b"
    r"(?:venues?|conferences?|journals?|workshops?|tracks?|"
    r"ICLR|NeurIPS|ICML|ACL|ARR|AAAI|CVPR)\b|"
    r"\b(?:meet|meets|meeting|fail|fails|failing)\b[^.!?\n]{0,80}\b"
    r"(?:bar|standards?|criteria)\b[^.!?\n]{0,60}\b"
    r"(?:venue|conference|publication|acceptance|ICLR|NeurIPS|ICML|ACL|ARR|"
    r"AAAI|CVPR)\b|"
    r"\b(?:above|below)\b[^.!?\n]{0,50}\b(?:bar|standard|criteria)\b"
    r"[^.!?\n]{0,60}\b(?:venue|conference|publication)\b|"
    r"\b(?:above|below)\b[^.!?\n]{0,50}\b(?:bar|standard|threshold|criteria)\b"
    r"[^.!?\n]{0,40}\b(?:for\s+)?(?:acceptance|publication)\b|"
    r"\b(?:does\s+not\s+|do\s+not\s+|fails?\s+to\s+)?"
    r"(?:meet|meets|meeting)\b"
    r"[^.!?\n]{0,40}\b(?:ICLR|NeurIPS|ICML|ACL|ARR|AAAI|CVPR)['’]?s?\s+"
    r"(?:bar|standards?|criteria)\b|"
    r"\b(?:falls?|falling)\s+(?:far\s+)?short\b[^.!?\n]{0,50}\b"
    r"(?:ICLR|NeurIPS|ICML|ACL|ARR|AAAI|CVPR)\b[^.!?\n]{0,30}\b"
    r"(?:bar|standards?|criteria)\b|"
    r"\b(?:falls?|falling)\s+(?:far\s+)?short\b[^.!?\n]{0,80}\b"
    r"publishable\b|"
    r"\bkeep(?:ing)?\s+(?:the\s+|my\s+)?current\s+(?:score|rating)\b|"
    r"\bmy\s+(?:(?:low|lower|high|higher)\s+)?"
    r"(?:initial|current|final|overall)\s+(?:score|rating)\b|"
    r"\b(?:give|giving|assign|assigns|assigned|assigning)\s+"
    r"(?:an?\s+)?(?:low|lower|high|higher)?\s*initial\s+(?:score|rating)\b|"
    r"\b(?:put|keep|keeping)\s+(?:a\s+|the\s+|my\s+)?"
    r"(?:low|lower|high|higher|current)\s+(?:score|rating)\b|"
    r"\b(?:adjust|adjusting|update|updating)\s+(?:the\s+|my\s+)?"
    r"final\s+(?:score|rating)\b|"
    r"\b(?:initial|current)\s+(?:score|rating)\s*(?:of|is|was|:|=)\s*"
    r"\d+(?:\.\d+)?(?:\s*/\s*\d+)?\b|"
    r"\b(?:recommend|vote)\s+(?:for\s+)?(?:acceptance|rejection|accept|reject)\b"
    r")"
)
_HISTORICAL_IDENTITY_RE = re.compile(
    r"(?i)(?:"
    r"\bmy\s+name\s+is\b|"
    r"\bi\s*(?:am|'m)\s+[A-Z][A-Za-z.'-]+(?:\s+[A-Z][A-Za-z.'-]+){1,3}"
    r"\s*,?\s*(?:an?\s+|the\s+)?reviewer\b|"
    r"\b(?:reviewer\s+(?:name|identity|id|identifier))\s*[:=]|"
    r"\bi\s+(?:(?:also|previously)\s+)?reviewed\s+this\s+"
    r"(?:paper|submission|work)\s+(?:for|during)\b|"
    r"\bi\s+served\s+as\s+(?:the|a)\s+reviewer\b|"
    r"\bi\s+was\s+invited\s+as\s+(?:an?\s+)?supplementary\s+reviewer\b|"
    r"\bi\b[^.!?\n]{0,45}\bdisclos(?:e|es|ed|ing)\b[^.!?\n]{0,80}\b"
    r"served\s+as\s+(?:one\s+of\s+)?(?:the|an?)\s+reviewers?\b|"
    r"\b(?:i\s+do\s+not|i\s+don['’]?t)\s+have\s+the\s+bandwidth\b"
    r"[^.!?\n]{0,100}\breview\s+workload\b|"
    r"\bdue\s+to\s+(?:the\s+)?limited\s+reviewing\s+time\b|"
    r"\b(?:the\s+)?reviewer\s+identifies\s+(?:this|the)\s+paper\b"
    r"[^.!?\n]{0,60}\bprevious\s+submission\b|"
    r"^\s*[-=_*\s]*previous\s+review[-=_*\s]*$|"
    r"\bas\s+a\s+reviewer\b[^.!?\n]{0,60}\bi\s+reviewed\s+"
    r"(?:this|the)\s+(?:paper|manuscript|submission|work)\b|"
    r"\b(?:papers?|submissions?)\s+in\s+my\s+(?:review\s+)?batch\b|"
    r"\b(?:compared|comparing)\s+(?:this|the)\s+(?:paper|submission|work)\b"
    r"[^.!?\n]{0,80}\bother\s+(?:papers?|submissions?)\s+in\s+my\s+batch\b|"
    r"\b(?:disclosure\s*:\s*)?i\b[^.!?\n]{0,60}\b"
    r"(?:learned|saw|discovered)\s+(?:the\s+)?authors?['’]?\s+names?\b|"
    r"\b(?:my|the)\s+previous\s+review\b|"
    r"\bas\s+a\s+reviewer\s+for\s+a\s+previous\s+submission\b|"
    r"\bbelow\s+is\s+my\s+previous\s+review\b|"
    r"\bi\s*(?:am|'m)\s+an?\s+"
    r"(?:researcher|student|professor|faculty\s+member|practitioner|domain\s+expert)\b|"
    r"\bi\s+(?:work|study)\s+at\b|"
    r"\bmy\s+affiliation\s+is\b|"
    r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b|"
    r"\bit\s+is\s+very\s+likely\s+that\b[^.!?\n]{0,240}\b"
    r"first\s+time\s+the\s+authors\s+are\s+submitting\s+to\s+(?:an?\s+)?"
    r"major\s+ML\s+conference\b[^.!?\n]{0,160}\b"
    r"authors\s+are\s+not\s+fluent\s+in\s+English\b|"
    r"\bi\s+have\s+[\*_`~]*personally[\*_`~]*\s+tried\s+before\s+"
    r"(?:an?\s+)?idea\b[^.!?\n]{0,280}\bnot\s+planning\s+to\s+publish\s+"
    r"this\s+idea\s+as\s+(?:an?\s+)?conference\s+paper\b"
    r")",
)
_HISTORICAL_PRIOR_REVIEW_RE = re.compile(
    r"(?i)(?:"
    r"\bi\s+(?:have\s+)?(?:also\s+|previously\s+)+reviewed\s+"
    r"(?:this|the)\s+(?:paper|manuscript|submission|work)\b|"
    r"\b(?:last\s+time\s+)?i\s+(?:have\s+)?"
    r"(?:(?:also|previously)\s+)?reviewed\s+"
    r"(?:(?:this|the)\s+)?(?:same\s+)?"
    r"(?:paper|manuscript|submission|work)\b[^.!?\n]{0,80}\b"
    r"(?:previously|before|again|for|at|during|"
    r"in\s+(?:(?:a\s+)?previous|ICLR|NeurIPS|ICML|ACL|ARR|AAAI|CVPR))\b|"
    r"\bi\s+(?:have\s+)?reviewed\s+"
    r"(?:(?:this|the)\s+)?(?:same(?:\s+exact)?|previous|prior|earlier)\s+"
    r"(?:paper|manuscript|submission|work|version|workshop\s+version)\b|"
    r"\bi\s+(?:have\s+)?reviewed\s+(?:a|an)\s+"
    r"(?:previous|prior|earlier)\s+(?:paper|manuscript|submission|work|version|"
    r"workshop\s+version)\b|"
    r"\b(?:last\s+time|previously)\s+i\s+reviewed\s+"
    r"(?:this|the)\s+(?:paper|manuscript|submission|work)\b"
    r"|\bi\s+(?:have\s+)?reviewed\s+(?:this|the)\s+"
    r"(?:paper|manuscript|submission|work)\s+in\s+the\s+past\b"
    r"|\bi\s+(?:have\s+)?reviewed\s+\d+\s*[-–—]\s*\d+\s+"
    r"(?:contemporaneous|similar|related)\s+(?:papers?|submissions?|works?)\b"
    r"|\b(?:have|having)\s+previously\s+reviewed\s+these\s+"
    r"(?:papers?|submissions?|works?)\b"
    r"|\bafter\s+(?:several|multiple|many)\s+reviewing\s+cycles\b|"
    r"\b(?:this|the)\s+paper\s+is\s+a\s+resubmission\s+from\s+"
    r"(?:an?\s+)?earlier\s+venue\b|"
    r"\b(?:in\s+fact,?\s+)?i\s+was\s+one\s+of\s+the\s+reviewers\s+of\s+"
    r"(?:this|the)\s+paper\b|"
    r"\b(?:this|the)\s+paper\s+is\s+also\s+submitted\s+in\s+"
    r"(?:ICLR|NeurIPS|ICML|ACL|AAAI|CVPR)\b[^.!?\n]{0,260}\b"
    r"reviewer\s+comments\b[^.!?\n]{0,220}|"
    r"\bwas\s+(?:this|the)\s+paper\s+submitted\s+elsewhere\s+first\b"
    r"|\b(?:compared\s+to|in\s+comparison\s+with)\s+(?:the\s+)?"
    r"previous\s+version\b"
    r"|\bi\s+have\s+compared\s+(?:the\s+)?previous\s+version\s+with\s+"
    r"(?:the\s+)?current\s+one\b"
    r"|\b(?:this|the)\s+version\b[^.!?\n]{0,70}\b"
    r"(?:previous|earlier)\s+submission\b"
    r"|\b(?:decision|outcome)\b[^.!?\n]{0,50}\bprevious\s+submission\b"
    r"|\bno\s+additional\s+questions\b[^.!?\n]{0,60}\b"
    r"previous\s+submission\b"
    r")"
)
_HISTORICAL_PRIOR_SUBMISSION_DETAIL_RE = re.compile(
    r"(?i)(?:"
    r"\b(?:this|the)\s+paper\s+(?:has\s+been|is|was)\s+"
    r"[\"'“”]?(?:also\s+)?submitted\s+(?:to|in)\s+"
    r"(?:ICLR|NeurIPS|ICML|ACL|AAAI|CVPR)(?:\s+\d{4})?[\"'“”]?\b|"
    r"\b(?:this|the)\s+code\s+is\s+[\"'“”]?submitted\s+to\s+"
    r"(?:ICLR|NeurIPS|ICML|ACL|AAAI|CVPR)(?:\s+\d{4})?[\"'“”]?|"
    r"\b(?:perhaps\s+)?(?:the\s+)?authors\s+initially\s+planned\s+to\s+"
    r"submit\s+to\s+(?:ICLR|NeurIPS|ICML|ACL|AAAI|CVPR)\b"
    r"(?:\s+and(?=\s+))?|"
    r"\b(?:this|the)\s+paper\s+was\s+previously\s+submitted\s+to\s+"
    r"(?:ICLR|NeurIPS|ICML|ACL|AAAI|CVPR),?\s+but(?=\s+)|"
    r"\bi\s+already\s+commented\s+to\s+the\s+AC\s+before\s+submitting\s+"
    r"the\s+review\s+regarding(?=\s+)"
    r"|\b(?:which|that)\s+is\s+also\s+submitted\s+to\s+"
    r"(?:ICLR|NeurIPS|ICML|ACL|AAAI|CVPR)(?:\s+\d{4})?\b"
    r"|\bgiven\s+(?:that\s+)?(?:this|the)"
    r"(?:\s+(?:paper|work|submission))?\s+"
    r"is\s+(?:a\s+)?resubmission,?\s*"
    r"|\bas\s+i\s+(?:have\s+)?said\s+in\s+(?:a|my)\s+previous\s+review"
    r"(?:\s+of\s+(?:this|the)\s+(?:same\s+)?paper)?\s*:\s*"
    r")"
)
_HISTORICAL_IDENTITY_FRAGMENT_RE = re.compile(
    r"(?ix)(?:"
    # Keep the technical judgement or requested clarification that follows a
    # reviewer self-assessment; only the reviewer-specific preface is private.
    r"\b(?:i\s+am\s+afraid\s+)?i\s*(?:am|'m)\s+"
    r"(?:(?:by\s+no\s+means|not)\s+)?(?:an?\s+)?"
    r"(?:expert|specialist)\b[^,;:.!?\n]{0,110}"
    r"(?:\s+so\s+my\s+review\s+(?:will|would)\s+have\s+"
    r"(?:pretty\s+|rather\s+|very\s+)?(?:low|lower)\s+confidence)?"
    r"\s*,?\s*(?:but\s+)?|"
    r"\b(?:even\s+as\s+)?(?:an?\s+)?non[-\s]expert\b\s*,?\s*|"
    r"\bmy\s+(?:review|assessment)\s+(?:has|will\s+have|would\s+have)\s+"
    r"(?:pretty\s+|rather\s+|very\s+)?(?:low|lower)\s+confidence\b"
    r"\s*,?\s*(?:but\s+)?|"
    r"\b(?:this|the)\s+paper\s+is\s+also\s+submitted\s+in\s+"
    r"(?:ICLR|NeurIPS|ICML|ACL|ARR|AAAI|CVPR)(?:\s+\d{2,4})?\s+and\s+"
    r"(?:ICLR|NeurIPS|ICML|ACL|ARR|AAAI|CVPR)(?:\s+\d{2,4})?\s+but\s+"
    r"without\s+replying\s+to\s+any\s+reviewer\s+comments\b|"
    r"\bi\s+have\s+personally\s+tried\s+before\s+an\s+idea\s+like\s+this,?\s+"
    r"although\s+i\s+am\s+currently\s+not\s+planning\s+to\s+publish\s+"
    r"this\s+idea\s+as\s+a\s+conference\s+paper\b"
    r")"
)
_HISTORICAL_PRIOR_CONTEXT_FRAGMENT_RE = re.compile(
    r"(?ix)(?:"
    r"\bi\s+(?:carefully\s+)?read\s+this\s+(?:new|revised)\s+version\b"
    r"\s*[,;:]?\s*|"
    r"\bwhich\s+was\s+explicit\s+in\s+(?:the\s+)?previous\s+version"
    r"(?:\s+of\s+(?:this|the)\s+paper)?\b\s*,?\s*(?:but\s+)?|"
    r"\bmost\s+reviewers\s+at\s+that\s+time\s+asked\s+|"
    r"\bafter\s+comparing\s+(?:the\s+)?current\s+version\s+with\s+"
    r"(?:the\s+)?earlier\s+submission\s*,?\s*|"
    r"\bi\s+reviewed\s+this\s+work\s+from\s+the\s+previous\s+"
    r"conference\s+cycle\s*;?\s*|"
    r"\braised\s+by\s+(?:the\s+)?reviewers\s+at\s+that\s+time\b|"
    r"\bfrom\s+(?:the\s+)?last\s+reviews?\b|"
    r"\bduring\s+(?:the\s+)?previous\s+round\s+of\s+reviews?\b|"
    r"\bi\s+remember\s+the\s+(?:answer|response)\s+from\s+(?:the\s+)?"
    r"authors?(?:\s+regarding\s+this\s+point)?\b|"
    r"\bin\s+this\s+version\s+compared\s+to\s+the\s+previous\s+one\b|"
    r"\bcompared\s+to\s+a\s+previous\s+draft\s+in\s+"
    r"(?:ICLR|NeurIPS|ICML|ACL|ARR|AAAI|CVPR)(?:\s+\d{2,4})?\s*,?\s*|"
    r"\bwhich\s+i\s+also\s+raised\s+as\s+a\s+reviewer\s+for\s+a\s+"
    r"previous\s+submission(?:\s+of\s+this\s+paper)?(?:\s+to\s+"
    r"(?:ICLR|NeurIPS|ICML|ACL|ARR|AAAI|CVPR)(?:\s+\d{2,4})?)?\s*,?\s*|"
    r"\bin\s+my\s+previous\s+review\s*,?\s*"
    r")"
)
_HISTORICAL_EXPLICIT_REVIEW_METADATA_RE = re.compile(
    r"(?i)(?:"
    r"(?<!\()\bi\b(?!\))[^.!?\n]{0,50}\b"
    r"(?:assign|give|set|provide|award|recommend)\s+"
    r"(?:an?\s+)?(?:positive|negative|borderline|conservative|tentative|"
    r"low|high)\s+(?:score|rating|evaluation|review)\b|"
    r"(?<!\()\bi\b(?!\))[^.!?\n]{0,50}\b"
    r"(?:lean|leaning)\s+(?:toward|towards)\s+(?:an?\s+)?"
    r"(?:positive|negative|borderline)\s+(?:score|rating|evaluation)\b|"
    r"(?<!\()\bi\b(?!\))[^.!?\n]{0,50}\bunable\s+to\b[^.!?\n]{0,30}\b"
    r"(?:positive|negative|borderline)\s+(?:score|rating)\b|"
    r"\b(?:hence|thus|therefore)\b[^.!?\n]{0,30}\b"
    r"(?:positive|negative|borderline)\s+(?:score|rating)\b|"
    r"\b(?:this|the)\s+(?:paper|submission|work|manuscript)\b"
    r"[^.!?\n]{0,40}\b(?:deserve|deserves|received|receives|gets?|has)\b"
    r"[^.!?\n]{0,20}\b(?:positive|negative|borderline\s+)?"
    r"(?:score|rating)\s+(?:of\s+)?\d+(?:\.\d+)?(?:\s*/\s*\d+)?\b|"
    r"\bthis\b[^.!?\n]{0,30}\b(?:deserve|deserves)\b[^.!?\n]{0,20}\b"
    r"(?:positive|negative|borderline)\s+(?:score|rating|evaluation)\b|"
    r"\b(?:this|the)\s+(?:paper|submission|work|manuscript)\b"
    r"[^.!?\n]{0,160}\b(?:deserve|deserves)\b[^.!?\n]{0,30}\b"
    r"(?:positive|negative|borderline)\s+(?:score|rating|evaluation)\b|"
    r"\b(?:would|will|can|could)\s+(?:strongly\s+)?recommend\b"
    r"[^.!?\n]{0,30}\b(?:positive|negative|borderline)\s+rating\b|"
    r"\b(?:this|the)\s+(?:paper|submission|work|manuscript)\b"
    r"[^.!?\n]{0,40}\b(?:deserve|deserves)\b[^.!?\n]{0,20}\b"
    r"(?:positive|negative|borderline)\s+(?:score|rating|evaluation)\b|"
    r"\b(?:my|this\s+review['’]?s?|the\s+reviewer['’]?s?)\b"
    r"[^.!?\n]{0,35}\b(?:main|current|initial|final|overall|tentative|"
    r"conservative|positive|negative|borderline|low|high)\s+"
    r"(?:score|rating)\b|"
    r"\b(?:soundness|presentation|contribution)\s*[:=]\s*\d+(?:\.\d+)?"
    r"[^.!?\n]{0,50}\boverall\s+(?:review\s+)?score\b|"
    r"\b(?:a\s+)?score\s+of\s+\d+(?:\.\d+)?(?:\s*/\s*\d+)?\s+"
    r"from\s+me\b|"
    r"\b(?:minor|slightly|tentative(?:ly)?|relatively)\s+"
    r"(?:positive|negative|low|high)\s+(?:score|rating)\b|"
    r"\b(?:support|supports|supporting)\s+(?:an?\s+)?acceptance\b|"
    r"(?<!\()\bi\b(?!\))[^.!?\n]{0,40}\b(?:gave|given)\s+"
    r"(?:this|the)\s+(?:paper|submission|work|manuscript)\s+"
    r"(?:an?\s+)?(?:accept|reject)\b|"
    r"\b(?:clear|definite|straightforward)\s+rejection\b|"
    r"\bfinal\s+desk[-\s]rejection\s+decision\b|"
    r"\b(?:this|the)\s+(?:paper|submission|work|manuscript)\b"
    r"[^.!?\n]{0,40}\b(?:is|was|has\s+been|would\s+be|should\s+be)\s+"
    r"(?:justifiably\s+|clearly\s+)?(?:accepted|rejected)\b|"
    r"\b(?:prevent|prevents|preventing|stops?)\b[^.!?\n]{0,60}\b"
    r"acceptance\b[^.!?\n]{0,40}\b(?:at|to|for)\s+(?:an?\s+|the\s+|this\s+)?"
    r"(?:venue|conference|journal|workshop|ICLR|NeurIPS|ICML|ACL|ARR|AAAI|CVPR)\b|"
    r"(?<!\()\bi\b(?!\))[^.!?\n]{0,80}\b"
    r"(?:suggest|suggesting)\b[^.!?\n]{0,40}\b"
    r"(?:clear\s+|weak\s+|strong\s+)?(?:accept|reject|acceptance|rejection)\b|"
    r"\bto\s+(?:accept|reject)\s+(?:this|the)\s+"
    r"(?:paper|submission|work|manuscript)\b|"
    r"\b(?:set|assign|reduce|raise|lower)\s+my\s+confidence"
    r"(?:\s+(?:score|rating|level))?\s+(?:to|as)\s+\d+(?:\.\d+)?\b|"
    r"\bmy\s+(?:review\s+)?(?:low|high)\s+confidence\b|"
    r"\bmy\s+review\s+(?:has|had|is|was)\s+(?:very\s+|relatively\s+)?"
    r"(?:low|high)\s+confidence\b|"
    r"\b(?:review|reviewer)\s+confidence\b[^.!?\n]{0,30}\b"
    r"(?:low|high|\d+(?:\.\d+)?)\b|"
    r"\b(?:low|high)\s+confidence\b[^.!?\n]{0,30}\b(?:my\s+)?review\b|"
    r"\bnon[-\s]expert\s+reviewer\b[^.!?\n]{0,30}\bconfidence\b"
    r"|\b(?:adjust|revise|change|update|reconsider)\s+my\s+future\s+"
    r"(?:score|rating|recommendation|vote)\b"
    r")"
)
_HISTORICAL_FIRST_PERSON_SCORE_RE = re.compile(
    r"(?i)(?:"
    r"(?<!\()\bi(?:['’]d)?\b(?!\))\s+"
    r"(?:(?:am|was|have|had|would|will|can|could|cannot|can't|do|don't|did|"
    r"still|currently|only|definitely|truly|now|also|rather|very|not|to|like|"
    r"decide|decided|inclined|be|willing|potentially)\s+){0,8}"
    r"(?:give|gave|given|giving|assign|assigned|assigning|put|select|selected|"
    r"improve|improved|reduce|reduced|raise|raised|lower|lowered|revise|revised|"
    r"adjust|adjusted|reconsider|reconsidered|provide|provided|go|gone|lean|"
    r"leaning|hold|held|start|started)\b"
    r"[^.!?\n]{0,70}\b(?:scores?|ratings?)\b|"
    r"\b(?:scores?|ratings?)\b[^.!?\n]{0,35}\b"
    r"(?<!\()i\b(?!\))[^.!?\n]{0,25}\b(?:give|gave|assigned|picked)\b|"
    r"\b(?:me|myself)\s+to\s+(?:give|assign|provide)\b"
    r"[^.!?\n]{0,50}\b(?:scores?|ratings?)\b|"
    r"\b(?:would|will|could|can)\s+be\s+(?:willing|inclined)\s+to\s+"
    r"(?:give|assign|provide)\b[^.!?\n]{0,50}\b(?:scores?|ratings?)\b|"
    r"(?<!\()\bi\b(?!\))[^.!?\n]{0,100}\b(?:scores?|ratings?)\b"
    r"[^.!?\n]{0,40}\b(?:go\s+up|rise|increase|improve|change)\b|"
    r"\b(?:the\s+reviewer|reviewer)\b[^.!?\n]{0,60}\b"
    r"(?:assign|give|select|put)\b[^.!?\n]{0,50}\b(?:score|rating)\b|"
    r"\b(?:results?|comparisons?|concerns?|weaknesses?|limitations?)\b"
    r"[^.!?\n]{0,80}\blead(?:s|ing)?\s+to\s+(?:an?\s+)?"
    r"(?:negative|positive|borderline)\s+rating\b|"
    r"\b(?:improvement|increase|revision|change)\s+of\s+"
    r"(?:my|the)\s+(?:score|rating)\b"
    r")"
)
_HISTORICAL_REVIEW_CONFIDENCE_RE = re.compile(
    r"(?i)(?:"
    r"\b(?:i|the\s+reviewer|reviewer)\b[^.!?\n]{0,100}\b"
    r"(?:low[-\s]?confidence\s+score|confidence\s+(?:score|rating|level))\b|"
    r"\b(?:i|the\s+reviewer|reviewer)\b[^.!?\n]{0,100}\b"
    r"(?:improve|reduce|lower|lowered|mark|marked|select|selected|assign|assigned)\b"
    r"[^.!?\n]{0,50}\bconfidence\b|"
    r"\bmy\s+review\b[^.!?\n]{0,80}\b(?:low|weak|high)\s+confidence\b|"
    r"\bmy\s+review\s+will\s+have\b[^.!?\n]{0,30}\bconfidence\b|"
    r"\b(?:my\s+review\s+and\s+score|review\s+and\s+score)\b"
    r"[^.!?\n]{0,50}\b(?:weak|low|high)\s+confidence\b|"
    r"\b(?:low|weak|high)\s+confidence\b[^.!?\n]{0,50}\b"
    r"(?:advocate\s+for\s+(?:this|the)\s+paper|review)\b|"
    r"\bfirst\s+time\s+reviewing\b[^.!?\n]{0,100}\b"
    r"(?:lower|reduce|adjust)\s+my\s+confidence\b|"
    r"\b(?:marked|selected)\s+(?:an?\s+)?low\s+(?:level\s+of\s+)?confidence\b|"
    r"\blowered\s+my\s+confidence\b|"
    r"\bi\s+never\s+have\s+(?:an?\s+)?[^.!?\n]{0,30}\bconfidence\s+"
    r"that\s+i\s+understand\b|"
    r"\bmy\s+main\s+area\s+of\s+research\b[^.!?\n]{0,260}\b"
    r"my\s+(?:relatively\s+)?low\s+confidence\b|"
    r"\bi\s+am\s+not\s+deeply\s+familiar\b[^.!?\n]{0,220}\b"
    r"(?:other\s+reviewers?|final\s+score)\b|"
    r"\bnot\s+fully\s+confident\s+in\s+my\s+own\s+judg(?:e)?ment\b|"
    r"\b(?:low|lower|high|higher)\s+(?:level\s+of\s+)?confidence\s+"
    r"in\s+my\s+review\b|"
    r"\bnot\s+confident\s+to\s+review\s+(?:this|the)\s+paper\b"
    r")"
)
_HISTORICAL_IMPLICIT_DECISION_RE = re.compile(
    r"(?i)(?:"
    r"\b(?:the\s+)?pros\s+(?:far\s+|substantially\s+)?outweigh\s+"
    r"(?:the\s+)?cons\b|"
    r"\b(?:the\s+)?(?:paper(?:['’]s)?\s+)?strengths\s+"
    r"(?:far\s+|substantially\s+)?outweigh\s+"
    r"(?:the\s+|its\s+)?weaknesses\b|"
    r"\bno(?:t\s+any)?\s+(?:significant\s+)?blocker\s+to\s+acceptance\b|"
    r"\b(?:solid|strong)\s+submission\s+for\s+"
    r"(?:ICLR|NeurIPS|ICML|ACL|ARR|AAAI|CVPR)\b"
    r")"
)
_HISTORICAL_REVIEW_VERDICT_RE = re.compile(
    r"(?i)(?:"
    r"\bone\s+could\s+consider\s+(?:raising|increasing|changing|revising)\s+"
    r"(?:the\s+)?(?:score|rating)\b|"
    r"\b(?:glad|happy|willing|prepared)\s+to\s+"
    r"(?:raise|increase|revise|change|update)\b[^.!?\n]{0,40}\b"
    r"(?:scores?|ratings?)\b|"
    r"\bwe\b[^.!?\n]{0,60}\b(?:give|provide|assign)\b"
    r"[^.!?\n]{0,40}\b(?:positive|negative|borderline)\s+"
    r"(?:score|rating)\b|"
    r"\b(?:this|the)\s+review\s+(?:has|had|carries|carried)\s+"
    r"(?:lower|low|higher|high|weak)\s+confidence\b|"
    r"\b(?:change|changing|revise|revising|reconsider|reconsidering)\s+"
    r"my\s+decision\b|"
    r"\b(?:reduce|reduces|reduced|reducing|lower|lowers|lowered|lowering)\s+"
    r"my\s+confidence\s+in\s+(?:accepting|rejecting)\b|"
    r"\b(?:glad|happy|pleased)\s+to\s+see\s+(?:this|the)\s+"
    r"(?:paper|submission|work|manuscript)\s+(?:be\s+)?accepted\b|"
    r"\b(?:i|the\s+reviewer|reviewer)\b[^.!?\n]{0,70}\b"
    r"(?:give|gives|gave|assign|assigns|assigned|recommend|recommends|"
    r"recommended|start|starts|started)\b[^.!?\n]{0,50}\b"
    r"(?:weak|strong|borderline)?\s*(?:acceptance|rejection|accept|reject)\b|"
    r"\bnot\s+ready\s+to\s+be\s+(?:accepted|published)\b|"
    r"\b(?:push|pushes|pushed|pushing)\s+for\s+"
    r"(?:strong\s+|weak\s+)?(?:acceptance|rejection)\b|"
    r"\boscillat(?:e|es|ed|ing)\s+between\b[^.!?\n]{0,60}\b"
    r"(?:acceptance|rejection|accept|reject)\b|"
    r"\b(?:i|we)\b[^.!?\n]{0,40}\bopen\s+to\s+"
    r"(?:accepting|rejecting)\b|"
    r"\b(?:i|we)\b[^.!?\n]{0,40}\bdesk[-\s]reject\s+"
    r"(?:this|the)\s+(?:paper|submission|work|manuscript)\b"
    r"|\b(?:i|we)\b[^.!?\n]{0,80}\b(?:not\s+)?oppose\s+acceptance\b"
    r"|\b(?:i|we)\b[^.!?\n]{0,80}\b(?:would|will|could|can)\s+not\s+"
    r"reject\s+(?:this|the)\s+(?:paper|submission|work|manuscript)\b"
    r"|\b(?:i|we)\b[^.!?\n]{0,100}\bopen\s+to\s+moving\b"
    r"[^.!?\n]{0,60}\b(?:accept|reject)\s+range\b"
    r"|\b(?:i|we)\b[^.!?\n]{0,80}\bcannot\s+give\b[^.!?\n]{0,40}\b"
    r"(?:acceptance|rejection)\s+recommendation\b"
    r"|\b(?:i|we)\b[^.!?\n]{0,120}\b"
    r"(?:support|supportive|advocate|encourage|push)\b[^.!?\n]{0,80}\b"
    r"(?:acceptance|rejection)\b"
    r"|\b(?:i|we)\b[^.!?\n]{0,80}\b(?:want|like)\s+to\s+see\s+"
    r"(?:this|the)\s+(?:paper|submission|work|manuscript)\s+accepted\b"
    r"|\b(?:i|we)\b[^.!?\n]{0,80}\b(?:cannot|can't|could\s+not|"
    r"would\s+not)\s+accept\s+(?:this|the)\s+"
    r"(?:paper|submission|work|manuscript)\b"
    r"|\b(?:i|we)\b[^.!?\n]{0,80}\b(?:cannot|can't|could\s+not|"
    r"would\s+not)\s+(?:yet\s+)?advocate\s+for\s+acceptance\b"
    r"|\b(?:i|we)\b[^.!?\n]{0,80}\b(?:recommend|recommended|recommending)\b"
    r"[^.!?\n]{0,60}\b(?:for\s+)?publication\b"
    r"|\b(?:i|we)\b[^.!?\n]{0,60}\bvoted?\s+for\s+desk[-\s]reject\b"
    r"|\b(?:i|we)\b[^.!?\n]{0,80}\bdefer\b[^.!?\n]{0,50}\b"
    r"acceptance\s+decisions?\b"
    r"|\b(?:obstacle|blocker|grounds?)\s+(?:to|for)\s+"
    r"(?:acceptance|rejection)\b"
    r"|\b(?:score|rating)\s+of\s+(?:this|the)\s+"
    r"(?:paper|submission|work|manuscript)\b[^.!?\n]{0,40}\b"
    r"tends?\s+toward(?:s)?\s+(?:acceptance|rejection)\b"
    r"|\b(?:do|does|did)\s+not\s+represent\s+grounds?\s+for\s+"
    r"(?:acceptance|rejection)\b"
    r"|\b(?:this|the)\s+(?:paper|submission|work|manuscript)\b"
    r"[^.!?\n]{0,80}\b(?:cannot|can't|should\s+not|shouldn't)\s+be\s+"
    r"accepted\b"
    r"|\b(?:this|the)\s+(?:paper|submission|work|manuscript)\b"
    r"[^.!?\n]{0,80}\bshould\s+be\s+given\s+(?:an?\s+)?"
    r"(?:weak|strong|borderline)\s+(?:acceptance|rejection)\b"
    r"|\b(?:this|the)\s+(?:paper|submission|work|manuscript)\s+is\s+"
    r"acceptable\s+for\s+publication\b"
    r"|\b(?:this|the)\s+(?:paper|submission|work|manuscript)\s+is\s+"
    r"publishable\b"
    r"|\b(?:difficult|hard)\s+for\s+(?:an?\s+)?"
    r"(?:clear|weak|strong|borderline)\s+(?:accept|reject)\b"
    r"|\b(?:strengths?|results?|contributions?)\b[^.!?\n]{0,100}\b"
    r"(?:foundation|basis|case)\s+for\s+(?:an?\s+|its\s+)?"
    r"(?:weak|strong|borderline)\s+(?:acceptance|rejection)\b"
    r"|\b(?:i|we)\b[^.!?\n]{0,80}\b(?:give|gave)\s+(?:an?\s+)?"
    r"(?:weakly|strongly|borderline)\s+(?:accept|reject)\b"
    r"|\b(?:i|we)\b[^.!?\n]{0,60}\b(?:give|gave|given)\b"
    r"[^.!?\n]{0,40}\b(?:clear|weak|strong|borderline)\s+"
    r"(?:accept|reject)\b"
    r"|\b(?:i|we)\b[^.!?\n]{0,100}\bchange\s+my\s+opinion\s+to\s+"
    r"(?:accept|reject)\b"
    r"|\b(?:before|until)\b[^.!?\n]{0,100}\b(?:i|we)\b"
    r"[^.!?\n]{0,60}\bgive\b[^.!?\n]{0,30}\b"
    r"(?:clear|weak|strong|borderline)\s+(?:accept|reject)\b"
    r"|\b(?:support|supporting|supportive)\s+(?:of\s+)?"
    r"(?:accepting|rejecting)\s+(?:this|the)\s+"
    r"(?:paper|submission|work|manuscript)\b"
    r"|\b(?:i|we)\b[^.!?\n]{0,80}\bsupport\b[^.!?\n]{0,60}\b"
    r"(?:paper|submission|work|manuscript)\s+to\s+be\s+"
    r"(?:accepted|rejected)\b"
    r"|\b(?:this|the)\s+reviewer\b[^.!?\n]{0,80}\b"
    r"(?:recommend|recommends|recommended)\s+(?:an?\s+)?"
    r"(?:acceptance|rejection|accept|reject)\b"
    r"|\bmy\s+acceptance\s+grade\b"
    r"|\bjustify\s+acceptance\s+to\s+(?:an?\s+|the\s+)?"
    r"(?:venue|conference|journal|workshop)\b"
    r"|\bfor\s+(?:this|the)\s+(?:paper|submission|work|manuscript)\s+"
    r"to\s+be\s+accepted\s+at\s+(?:an?\s+|the\s+|this\s+)?"
    r"(?:venue|conference|journal|workshop)\b"
    r"|\bbefore\s+(?:i|we)\s+can\s+commit\s+to\s+(?:an?\s+)?"
    r"(?:acceptance|rejection)\b"
    r"|\b(?:it|this|the\s+work)\b[^.!?\n]{0,70}\bshould\s+not\s+be\s+"
    r"accepted\s+by\s+(?:an?\s+|the\s+)?(?:top(?:[-\s]tier)?\s+)?"
    r"(?:venue|conference|journal|workshop)\b"
    r"|\b(?:i|we)\s+(?:do\s+not|don't|cannot|can't)\s+think\b"
    r"[^.!?\n]{0,70}\b(?:it|this|the\s+work)\s+should\s+be\s+accepted\s+"
    r"by\s+(?:an?\s+|the\s+)?(?:top(?:[-\s]tier)?\s+)?"
    r"(?:venue|conference|journal|workshop)\b"
    r")"
)
_HISTORICAL_VENUE_FIT_FRAGMENT_RE = re.compile(
    r"(?ix)(?:"
    # Standalone headings and venue-only prefixes carry no manuscript evidence.
    r"^\s*(?:[-*+]\s*)?(?:\*\*)?(?:venue\s+suitability\s*\(\s*)?"
    r"(?:relevance\s+to\s+(?:ICLR|NeurIPS|ICML|ACL|ARR|AAAI|CVPR)|"
    r"(?:ICLR|NeurIPS|ICML|ACL|ARR|AAAI|CVPR)\s+(?:fit|relevance))"
    r"(?:\s*\))?(?:\*\*)?\s*:?\s*$|"
    r"^\s*[-*+\s]*(?:\*\*)?overall\s+fit\s*\([^)]*"
    r"(?:ICLR|NeurIPS|ICML|ACL|ARR|AAAI|CVPR)[^)]*\)\s*:?"
    r"(?:\*\*)?\s*|"
    r"\b(?:in\s+(?:its|the)\s+current\s+state,?\s+)?"
    r"(?:this|the)\s+(?:paper|work|submission|contribution)\s+"
    r"(?:does\s+not|fails?\s+to)\s+meet\s+(?:the\s+)?standards?\s+of\s+"
    r"(?:an?\s+)?[^,.!?;\n]{0,70}\b(?:contribution\s+)?suitable\s+for\s+"
    r"(?:ICLR|NeurIPS|ICML|ACL|ARR|AAAI|CVPR)\b|"
    r"\b(?:this|the)\s+paper\s+is\s+arguably\s+a\s+better\s+fit\s+for\s+"
    r"(?:an?\s+)?[^,.!?;\n]{0,90}\b(?:conference|venue|journal)\s+than\s+"
    r"(?:an?\s+)?[^,.!?;\n]{0,55}\b(?:conference|venue|journal)\s+like\s+"
    r"(?:ICLR|NeurIPS|ICML|ACL|ARR|AAAI|CVPR),?\s*(?:where\s+)?|"
    r"\bto\s+strengthen\s+(?:its|the\s+paper['’]s)\s+fit\s+for\s+"
    r"(?:ICLR|NeurIPS|ICML|ACL|ARR|AAAI|CVPR),?\s*|"
    r"\badequation\s+of\s+(?:the\s+)?contribution\s+to\s+(?:the\s+)?"
    r"(?:ICLR|NeurIPS|ICML|ACL|ARR|AAAI|CVPR)\s+conference\b|"
    # Direct scope/alignment questions or conclusions. Requiring a named venue
    # (or an explicitly intended venue) keeps ordinary technical uses of
    # ``scope`` and ``fit`` untouched.
    r"\b(?:apart\s+from\b[^.!?\n]{0,90}\b)?"
    r"(?:do\s+you\s+think\s+|does\s+|do\s+|would\s+|could\s+|can\s+|"
    r"may\s+|might\s+|whether\s+|why\s+|how\s+|if\s+)?"
    r"(?:this|the)\s+(?:paper|work|submission|contribution|resource)\b"
    r"[^.!?\n]{0,80}\b(?:fit|fits|fitting|aligned|aligns|alignment|"
    r"suitability)\b[^.!?\n]{0,60}\b(?:scope\s+of\s+)?"
    r"(?:ICLR|NeurIPS|ICML|ACL|ARR|AAAI|CVPR|the\s+intended\s+venue)"
    r"(?!['’]s\s+ethical\s+standards?\b)"
    r"(?:['’]s)?(?!\s+ethical\s+standards?\b)"
    r"(?:\s+(?:scope|venue|conference|audience))?\b|"
    r"\b(?:main\s+concern\b[^.!?\n]{0,60}\b|questions?\b[^.!?\n]{0,60}\b|"
    r"doubts?\b[^.!?\n]{0,40}\b)?whether\b[^.!?\n]{0,80}\b"
    r"(?:fit|fits|fitting|falls?|suitability)\b[^.!?\n]{0,60}\b"
    r"(?:ICLR|NeurIPS|ICML|ACL|ARR|AAAI|CVPR|the\s+intended\s+venue)"
    r"(?:['’]s)?(?:\s+scope)?\b|"
    r"\b(?:out\s+of|within)\s+"
    r"(?:ICLR|NeurIPS|ICML|ACL|ARR|AAAI|CVPR)(?:['’]s)?\s+scope\b|"
    r"\b(?:less|more)\s+aligned\s+with\s+"
    r"(?:ICLR|NeurIPS|ICML|ACL|ARR|AAAI|CVPR)(?:['’]s)?(?:\s+(?:core\s+)?scope)?\b|"
    r"\b(?:aligned|alignment)\s+with\s+"
    r"(?:ICLR|NeurIPS|ICML|ACL|ARR|AAAI|CVPR)(?:['’]s)?\s+(?:core\s+)?scope\b|"
    r"\b(?:ICLR|NeurIPS|ICML|ACL|ARR|AAAI|CVPR)\s+scope\s+concern\s*:\s*|"
    # Relevance, audience, and niche are historical venue judgements only when
    # explicitly tied to a named venue/community.
    r"\b(?:degree\s+of\s+)?relevance\s+to\s+(?:the\s+)?"
    r"(?:ICLR|NeurIPS|ICML|ACL|ARR|AAAI|CVPR)(?:\s+research)?\s+"
    r"(?:community|audience)\b|"
    r"\b(?:general|broad)\s+interest\s+for\s+(?:the\s+)?"
    r"(?:ICLR|NeurIPS|ICML|ACL|ARR|AAAI|CVPR)(?:\s+research)?\s+"
    r"(?:community|audience)\b|"
    r"\b(?:only\s+)?(?:the\s+)?(?:first|second|third|last|former|latter)\s+is\s+"
    r"relevant\s+to\s+(?:the\s+)?"
    r"(?:ICLR|NeurIPS|ICML|ACL|ARR|AAAI|CVPR)\s+(?:community|audience)\b|"
    r"\b(?:general|broad|limited|little|no|unclear|questionable)\s+interest\b"
    r"[^.!?\n]{0,60}\b(?:ICLR|NeurIPS|ICML|ACL|ARR|AAAI|CVPR)\s+"
    r"(?:community|audience)\b|"
    r"\b(?:not|less)\s+(?:particularly\s+|clearly\s+)?relevant\s+for\s+"
    r"(?:the\s+)?(?:ICLR|NeurIPS|ICML|ACL|ARR|AAAI|CVPR)\b|"
    r"\b(?:not\s+sure\s+(?:if|whether)|unsure\s+(?:if|whether))\b"
    r"[^.!?\n]{0,60}\brelevant\s+for\s+"
    r"(?:the\s+)?(?:ICLR|NeurIPS|ICML|ACL|ARR|AAAI|CVPR)\b|"
    r"\b(?:not|less)\s+(?:the\s+)?(?:most\s+)?relevant\s+"
    r"(?:topic\s+)?for\s+(?:ICLR|NeurIPS|ICML|ACL|ARR|AAAI|CVPR)\s+"
    r"researchers?\b|"
    r"\b(?:not|only|unlikely\s+to\s+be|may\s+not\s+be)\b"
    r"[^.!?\n]{0,60}\b(?:interest|interesting)\b[^.!?\n]{0,40}\b"
    r"(?:ICLR|NeurIPS|ICML|ACL|ARR|AAAI|CVPR)\s+(?:community|audience)\b|"
    r"\b(?:unsure|uncertain|cannot\s+judge|can['’]?t\s+judge|doubt)\b"
    r"[^.!?\n]{0,80}\b(?:interest|relevance)\b[^.!?\n]{0,50}\b"
    r"(?:ICLR|NeurIPS|ICML|ACL|ARR|AAAI|CVPR)\s+(?:community|audience)\b|"
    r"\b(?:this|the)\s+(?:paper|work|field|problem|topic|submission)\b"
    r"[^.!?\n]{0,55}[‘’'\"]?\bniche\b[‘’'\"]?[^.!?\n]{0,35}\b"
    r"(?:ICLR|NeurIPS|ICML|ACL|ARR|AAAI|CVPR)(?:['’]s)?"
    r"(?:\s+(?:broader\s+ML\s+)?audience|\s+conference)?\b|"
    r"\b[‘’'\"]?niche[‘’'\"]?\s+(?:topic\s+)?at\s+"
    r"(?:ICLR|NeurIPS|ICML|ACL|ARR|AAAI|CVPR)\b|"
    r"\b(?:ICLR|NeurIPS|ICML|ACL|ARR|AAAI|CVPR)\s+audience\b"
    r"[^.!?\n]{0,65}\b(?:find|finds|would\s+find)\b[^.!?\n]{0,55}\b"
    r"(?:more|less)\s+relevant\b|"
    r"\b(?:why|whether)\b[^.!?\n]{0,60}\b(?:this|the)\s+paper\s+is\s+"
    r"relevant\s+for\s+(?:the\s+)?"
    r"(?:ICLR|NeurIPS|ICML|ACL|ARR|AAAI|CVPR)\s+(?:community|audience)\b|"
    r"\b(?:interest|relevan\w*|impact|appeal|value|significan\w*|"
    r"importan\w*|useful|beneficial|accessible|familiar|insights?|contribution|"
    r"offer(?:s|ed)?|limited|narrow)\b[^,.!?;\n]{0,120}\b"
    r"(?:general|broader|main|core|wider|vast|current)?\s*"
    r"(?:ICLR|NeurIPS|ICML|ACL|ARR|AAAI|CVPR)\s+"
    r"(?:community|audience|readership|attendees?|researchers?)\b|"
    r"\b(?:ICLR|NeurIPS|ICML|ACL|ARR|AAAI|CVPR)\s+"
    r"(?:community|audience|readership|attendees?|researchers?)\b"
    r"[^,.!?;\n]{0,100}\b(?:interest|relevan\w*|impact|appeal|value|"
    r"significan\w*|important|useful|beneficial|accessible|familiar|insights?)\b|"
    r"\b(?:limited\s+scope\s+and\s+|lacks?\s+(?:importance\s+and\s+)?)"
    r"interest\s+(?:to|in|for)\s+(?:the\s+)?"
    r"(?:ICLR|NeurIPS|ICML|ACL|ARR|AAAI|CVPR)\s+(?:community|audience)\b|"
    r"\b(?:finance|quantitative\s+modeling|database|systems?|physics|medical|"
    r"health\s+informatics|computer\s+vision)\s+(?:researchers?|community)\b"
    r"[^.!?\n]{0,90}\brather\s+than\s+(?:the\s+)?"
    r"(?:ICLR|NeurIPS|ICML|ACL|ARR|AAAI|CVPR)\s+audience\b|"
    # Named-venue thresholds and placement conclusions. Match the venue suffix
    # where possible so the underlying technical criticism remains available.
    r"\bfor\s+(?:the\s+)?(?:core\s+)?(?:ML\s+)?community,?\s+such\s+as\s+"
    r"(?:ICLR|NeurIPS|ICML|ACL|ARR|AAAI|CVPR),?\b[^.!?\n]{0,50}\b"
    r"(?:little|limited|no)\s+interest\b|"
    r"\b(?:does\s+not|do\s+not|cannot|can['’]?t)\s+find\s+(?:its\s+)?place\s+in\s+"
    r"(?:ICLR|NeurIPS|ICML|ACL|ARR|AAAI|CVPR)(?:['’]?\d{2,4})?\b|"
    r"\b(?:a\s+bit\s+)?out\s+of\s+(?:the\s+)?scope\s+of\s+"
    r"(?:ICLR|NeurIPS|ICML|ACL|ARR|AAAI|CVPR)\b|"
    r"\b(?:not\s+)?(?:significant|novel|important|strong)\s+enough\s+for\s+"
    r"(?:the\s+)?(?:main\s+)?(?:ICLR|NeurIPS|ICML|ACL|ARR|AAAI|CVPR)"
    r"(?:\s+conference)?\b|"
    r"\b(?:modest|incremental)\s+for\s+"
    r"(?:ICLR|NeurIPS|ICML|ACL|ARR|AAAI|CVPR)\b|"
    r"\bfor\s+(?:a\s+)?top[-\s]tier\s+venue\s+like\s+"
    r"(?:ICLR|NeurIPS|ICML|ACL|ARR|AAAI|CVPR)\b|"
    r"\b(?:for|at)\s+(?:a\s+)?(?:venue|conference)\s+like\s+"
    r"(?:ICLR|NeurIPS|ICML|ACL|ARR|AAAI|CVPR)\b|"
    r"\b(?:to\s+)?(?:reach|clear|meet|pass|fall(?:s|ing)?\s+short\s+of|"
    r"far\s+from\s+reaching)\b[^.!?\n]{0,35}\b(?:the\s+)?"
    r"(?:expected\s+|typical\s+|high\s+)?(?:bar|standard|criteria)\b"
    r"\s+(?:of|for|required\s+for)\s+"
    r"(?:ICLR|NeurIPS|ICML|ACL|ARR|AAAI|CVPR)\b|"
    r"\b(?:falls?|falling)\s+(?:far\s+)?(?:below|short\s+of)\s+"
    r"(?:the\s+)?(?:expected\s+|typical\s+|high\s+)?(?:bar|standards?)\s+"
    r"(?:of|for|at)\s+(?:ICLR|NeurIPS|ICML|ACL|ARR|AAAI|CVPR)\b|"
    r"\b(?:acceptable|sufficient|expected)\s+(?:quality|standard)\s+at\s+"
    r"(?:a\s+)?venue\s+like\s+"
    r"(?:ICLR|NeurIPS|ICML|ACL|ARR|AAAI|CVPR)\b|"
    r"\bfor\s+publication\s+at\s+"
    r"(?:ICLR|NeurIPS|ICML|ACL|ARR|AAAI|CVPR)\b|"
    r"\b(?:below|beneath)\s+(?:the\s+)?"
    r"(?:ICLR|NeurIPS|ICML|ACL|ARR|AAAI|CVPR)\s+standard\b|"
    r"\bto\s+(?:ICLR|NeurIPS|ICML|ACL|ARR|AAAI|CVPR)\s+topics\b|"
    r"\b(?:since|as)\s+(?:this|the)\s+submission\s+is\s+intended\s+for\s+"
    r"(?:ICLR|NeurIPS|ICML|ACL|ARR|AAAI|CVPR),?\s*|"
    r"\b(?:which|and\s+this)\s+would\s+make\s+it\s+out\s+of\s+"
    r"(?:ICLR|NeurIPS|ICML|ACL|ARR|AAAI|CVPR)(?:['’]s)?\s+scope\b|"
    r"\b(?:yet|but|and)\s+it\s+lacks\b[^.!?\n]{0,80}\b"
    r"aligned\s+with\s+(?:ICLR|NeurIPS|ICML|ACL|ARR|AAAI|CVPR)(?:['’]s)?\s+"
    r"(?:core\s+)?scope\b|"
    r"\b(?:ICLR|NeurIPS|ICML|ACL|ARR|AAAI|CVPR)\b[^.!?\n]{0,45}\b"
    r"wrong\s+venue\b[^.!?\n]{0,100}\b(?:journal|conference|venue)\b|"
    r"\bi\s+personally\s+do\s+not\s+think\s+it\s+is\s+a\s+good\s+fit\b"
    r"[^.!?\n]{0,180}\b(?:COLT|CCC|TMLR|DMLR|AAAI|KDD|WWW|SIGIR|EC|ACL|"
    r"EMNLP|NAACL|HPCA|ISCA|MICRO)\b[^.!?\n]{0,100}|"
    r"\b(?:and\s+)?doubt\s+(?:its|the\s+paper['’]s)\s+suitability\s+for\s+"
    r"(?:ICLR|NeurIPS|ICML|ACL|ARR|AAAI|CVPR)\b"
    r")"
)
_HISTORICAL_ALTERNATE_VENUE_RE = re.compile(
    r"(?i)(?:"
    r"(?:,?\s*and\s+)?submit\s+to\s+(?:a\s+)?future\s+venue\b|"
    r"\bmore\s+suitable\s+for\s+(?:a\s+)?more\s+focused\s+venue"
    r"(?:\s*\([^)]*\))?|"
    r"\bbeyond\s+(?:ICLR|NeurIPS|ICML|ACL|ARR|AAAI|CVPR),?"
    r"[^.!?\n]{0,90}\b(?:paper|work|it)\s+would\s+be\s+received\s+well\s+at\s+"
    r"(?:conferences?|journals?)\b[^.!?\n]{0,150}|"
    r"\bconsider\s+another\s+venue\b|"
    r"\bmore\s+naturally\s+aligned\s+with\s+(?:the\s+)?[*_]*"
    r"[A-Za-z][A-Za-z -]{1,45}[*_]*\s+community\s*\([^)]*"
    r"\b(?:ICLR|NeurIPS|ICML|ACL|ARR|AAAI|CVPR)\b[^)]*\)|"
    r"\b(?:of\s+)?greater\s+interest\s+to\s+communities\s+such\s+as\s+"
    r"(?-i:[A-Z][A-Z0-9-]{1,12})(?:\s+(?:and|or)\s+"
    r"(?-i:[A-Z][A-Z0-9-]{1,12}))+\b|"
    r"\b(?:(?-i:[A-Z][A-Z0-9-]{1,12}),\s+){1,4}(?:and\s+)?similar\s+"
    r"conferences\s+may\s+find\s+more\s+alignment\b|"
    r"\b(?:it|this\s+paper|the\s+work)\s+would\s+be\s+more\s+suitable\s+"
    r"for\s+publication\s+in\s+(?:for\s+example\s+)?"
    r"(?:TMLR|DMLR|JMLR|[A-Z][A-Z0-9-]{1,12})\b|"
    r"\bmore\s+suitable\s+for\s+(?:an?\s+|the\s+)?"
    r"(?:DMLR|TMLR|AAAI|KDD|WWW|SIGIR|EC|ACL|EMNLP|NAACL|HPCA|ISCA|MICRO|"
    r"specialized\s+journal|benchmark[-\s]focused\s+venue)\b|"
    r"\b(?:DMLR|TMLR|AAAI|KDD|WWW|SIGIR|EC|ACL|EMNLP|NAACL|HPCA|ISCA|"
    r"MICRO|journals?\s+like\s+(?:DMLR|TMLR))\b[^.!?\n]{0,30}\b"
    r"would\s+be\s+(?:an?\s+)?(?:better\s+venue|more\s+fitting)\b|"
    r"\b(?:fit|fits|fitting)\s+better\s+(?:at|in|for)\b[^.!?\n]{0,100}\b"
    r"(?:venues?|conferences?|journals?|workshops?|tracks?|DMLR|TMLR|AAAI|"
    r"KDD|WWW|SIGIR|EC|ACL|EMNLP|NAACL|HPCA|ISCA|MICRO)\b|"
    r"\bbetter\s+(?:placed|suited|aligned)\s+(?:at|in|for|with)\b"
    r"[^.!?\n]{0,100}\b(?:venues?|conferences?|journals?|workshops?|tracks?|"
    r"DMLR|TMLR|AAAI|KDD|WWW|SIGIR|EC|ACL|EMNLP|NAACL|HPCA|ISCA|MICRO)\b|"
    r"\b(?:align|aligned|aligns)\s+(?:more\s+)?(?:closely|naturally)\s+with\b"
    r"[^.!?\n]{0,120}\b(?:venues?|conferences?|journals?|workshops?|tracks?|"
    r"DMLR|TMLR|AAAI|KDD|WWW|SIGIR|EC|ACL|EMNLP|NAACL|HPCA|ISCA|MICRO)\b"
    r"(?:[^.!?\n]{0,100}\brather\s+than\s+"
    r"(?:ICLR|NeurIPS|ICML|ACL|AAAI))?|"
    r"\b(?:feel\s+|seem\s+|appears?\s+)?more\s+aligned\s+with\b"
    r"[^.!?\n]{0,120}\b(?:venues?|conferences?|journals?|workshops?|tracks?|"
    r"DMLR|TMLR|AAAI|KDD|WWW|SIGIR|EC|ACL|EMNLP|NAACL|HPCA|ISCA|MICRO)\b"
    r"(?:[^.!?\n]{0,100}\brather\s+than\s+"
    r"(?:ICLR|NeurIPS|ICML|ACL|AAAI))?|"
    r"\b(?:ICLR|NeurIPS|ICML|ACL|ARR|AAAI|CVPR)\s+is\s+"
    r"(?:not\s+)?(?:the\s+)?(?:proper|reasonable|best|right)\s+"
    r"(?:venue|choice|avenue|home)\b|"
    r"\b(?:not\s+convinced|not\s+sure|unsure)\b[^.!?\n]{0,60}\b"
    r"(?:ICLR|NeurIPS|ICML|ACL|ARR|AAAI|CVPR)\s+is\s+"
    r"(?:the\s+)?(?:best|proper|right|reasonable)\s+"
    r"(?:venue|choice|avenue|home)\b|"
    r"\b(?:i|we)\s+(?:do\s+not|don't)\s+think\b[^.!?\n]{0,40}\b"
    r"(?:ICLR|NeurIPS|ICML|ACL|ARR|AAAI|CVPR)\s+is\s+"
    r"(?:an?\s+|the\s+)?(?:proper|reasonable|best|right)\s+"
    r"(?:venue|choice|avenue|home)\b|"
    r"\b(?:paper|work|submission|contribution)\b[^.!?\n]{0,80}\b"
    r"(?:should|could|would|might|may)\s+be\s+(?:better\s+)?"
    r"(?:published|positioned|presented|placed)\s+(?:as|at|in|to)\b"
    r"[^.!?\n]{0,100}\b(?:venues?|conferences?|journals?|workshops?|tracks?|"
    r"report|blog\s+post|demo\s+paper|DMLR|TMLR|AAAI|KDD|WWW|SIGIR|EC|ACL|"
    r"EMNLP|NAACL|HPCA|ISCA|MICRO)\b|"
    r"\b(?:best\s+thing\s+for\s+)?(?:this|the)\s+"
    r"(?:paper|work|submission|contribution)\b[^.!?\n]{0,60}\b"
    r"would\s+be\s+to\s+be\s+published\s+in\s+"
    r"(?:an?\s+)?(?:IEEE\s+)?journal\b|"
    r"\b(?:paper|work|submission|contribution)\b[^.!?\n]{0,40}\b"
    r"(?:better\s+suited\s+as|more\s+appropriate\s+to\s+position\s+it\s+as)\s+"
    r"(?:an?\s+)?(?:report|blog\s+post|demo\s+paper)\b|"
    r"\bmore\s+appropriate\s+to\s+position\s+it\s+as\s+"
    r"(?:an?\s+)?demo\s+paper\b|"
    r"\bbetter\s+suited\s+to\b[^.!?\n]{0,80}\b"
    r"(?:DMLR|TMLR|AAAI|KDD|WWW|SIGIR|EC|ACL|EMNLP|NAACL|HPCA|ISCA|MICRO)\b|"
    r"\b(?:amount|depth)\s+of\s+theoretical\s+information\b"
    r"[^.!?\n]{0,100}\bbetter\s+be\s+achieved\s+in\s+"
    r"(?:an?\s+)?specialized\s+journal\b|"
    r"\b(?:more|better)\s+aligned\s+with\s+(?:the\s+)?field\s+of\b"
    r"[^.!?\n]{0,100}\b(?:KDD|WWW|SIGIR|EC|ACL|EMNLP|NAACL|HPCA|ISCA|MICRO)\b|"
    r"\b(?:fit|fits)\s+better\b[^.!?\n]{0,60}\b"
    r"(?:dataset|benchmark)\s+tracks?\b[^.!?\n]{0,50}\b"
    r"rather\s+(?:than\s+)?(?:an?\s+|the\s+)?(?:research\s+)?main\s+track\b|"
    r"\b(?:paper|work|submission)\b[^.!?\n]{0,100}\bshould\s+be\s+published\b"
    r"[^.!?\n]{0,80}\bright\s+audience\b|"
    r"\b(?:venues?|conferences?|journals?|workshops?)\b"
    r"[^.!?\n]{0,50}\b(?:better|more)\s+(?:a\s+)?"
    r"(?:fit|fitting|suited|suitable|appropriate)\b|"
    r"\b(?:align|aligns|aligned)\s+more\s+closely\b[^.!?\n]{0,100}\b"
    r"(?:track|venue|conference|journal|workshop)\b[^.!?\n]{0,60}\b"
    r"(?:rather\s+than|instead\s+of)\b|"
    r"\bwhy\b[^.!?\n]{0,50}\b(?:submit|submitted|submission)\b"
    r"[^.!?\n]{0,80}\b(?:track|venue|conference|journal|workshop)\b|"
    r"\bwhy\b[^.!?\n]{0,40}\b(?:main\s+)?(?:conference|track)\b"
    r"[^.!?\n]{0,60}\b(?:rather\s+than|instead\s+of)\b|"
    r"\bunsuitable\s+submission\s+track\b|"
    r"\b(?:ICLR|NeurIPS|ICML|ACL|ARR|AAAI|CVPR|this\s+conference)\b"
    r"[^.!?\n]{0,60}\b(?:not|isn't|is\s+not)\b[^.!?\n]{0,30}\b"
    r"(?:right\s+(?:audience|avenue)|appropriate\s+audience)\b|"
    r"(?<!\()\bi\b(?!\))[^.!?\n]{0,40}\b(?:do\s+not|don't)\s+believe\b"
    r"[^.!?\n]{0,50}\b(?:ICLR|NeurIPS|ICML|ACL|ARR|AAAI|CVPR|"
    r"this\s+(?:venue|conference))\b[^.!?\n]{0,40}\b"
    r"(?:right\s+(?:audience|avenue|venue)|appropriate\s+audience)\b|"
    r"\b(?:robotics|theoretical\s+CS|software\s+testing|statistics|"
    r"numerical\s+analysis)\s+(?:venue|conference|journal)\b"
    r"[^.!?\n]{0,50}\b(?:better|more)\s+(?:a\s+)?"
    r"(?:fit|fitting|suited|suitable|appropriate)\b|"
    r"\b(?:ICLR|NeurIPS|ICML|ACL|ARR|AAAI|CVPR)\b[^.!?\n]{0,40}\b"
    r"(?:best|better)\s+(?:venue|home)\b|"
    r"\b(?:best|better)\s+(?:venue|home)\b[^.!?\n]{0,40}\b"
    r"(?:ICLR|NeurIPS|ICML|ACL|ARR|AAAI|CVPR)\b|"
    r"\b(?:this|the|current)\s+(?:draft|paper|submission|work|manuscript)\b"
    r"[^.!?\n]{0,60}\bshould\s+be\s+submitted\s+to\b[^.!?\n]{0,80}\binstead\b|"
    r"\bvenue[-\s]fit\s+perspective\b|"
    r"\bhard\s+to\s+find\s+the\s+value\b[^.!?\n]{0,100}\b"
    r"(?:on|for|at)\s+(?:this|the)\s+(?:conference\s+)?venue\b|"
    r"\binhibit(?:s|ed|ing)?\s+the\s+potential\b[^.!?\n]{0,80}\b"
    r"published\s+in\b[^.!?\n]{0,50}\b(?:top\s+)?(?:venue|conference)\b|"
    r"\bmajor\s+flaw\b[^.!?\n]{0,60}\bsubmission\s+to\s+"
    r"(?:an?\s+|the\s+)?(?:top[-\s]tier\s+)?(?:venue|conference)\b|"
    r"\b(?:insufficient|not\s+enough)\b[^.!?\n]{0,70}\b"
    r"justify\s+acceptance\b[^.!?\n]{0,40}\b(?:at|for)\s+"
    r"(?:this|the)\s+(?:venue|conference)\b|"
    r"\b(?:it\s+seems\s+better\s+to\s+me\s+that\s+)?the\s+paper\s+"
    r"is\s+submitted\s+to\s+(?:some\s+)?benchmark\s+tracks?\b|"
    r"\bICLR\s+papers?\b[^.!?\n]{0,180}\bout\s+of\s+scope\s+for\s+ICLR\b"
    r"\s*[-–—,:;]*|"
    r"\b(?:i|we)\s+(?:propose|suggest|recommend|encourage)\b"
    r"[^.!?\n]{0,80}\b(?:resubmit|be\s+resubmitted)\b[^.!?\n]{0,100}\b"
    r"(?:VLDB|KDD|other\s+venues?|"
    r"another\s+(?:conference\s+or\s+journal|conference|journal|venue)|"
    r"dataset\s+or\s+benchmark\s+track)\b|"
    r"\b(?:i|we)\s+(?:do\s+not|don't)\s+think\s+(?:this|the)\s+paper\s+"
    r"can\s+be\s+published\s+at\s+(?:ICLR|NeurIPS|ICML|ACL|AAAI|CVPR)\b|"
    r"\bthis\s+mismatch,\s+plus\s+how\s+different\s+this\s+paper\s+is\s+"
    r"from\s+the\s+rest\s+of\s+the\s+papers\b[^.!?\n]{0,100}\b"
    r"(?:contribution/)?venue\s+fit\b|"
    r"\bvenue\s+fit\s+is\s+(?:very\s+)?(?:good|poor|strong|weak)\b|"
    r"\bpublishing\s+(?:this|the)\s+paper\s+in\s+(?:an?\s+)?journal\s+"
    r"could\s+be\s+an?\s+alternative\s+to\s+(?:an?\s+)?conference\b|"
    r"\b(?:this|the)\s+paper\s+would\s+likely\s+(?:get|be)\s+rejected\s+at\s+"
    r"[^.!?\n]{0,100}\bconferences?\b[^.!?\n]{0,100}\b"
    r"published\s+at\s+(?:ICLR|NeurIPS|ICML|ACL|AAAI|CVPR)\b|"
    r"\b(?:my\s+primary\s+concern\s+is\s+that\s+i\s+think\s+)?"
    r"(?:this|the)\s+paper\s+belongs\s+in\s+(?:an?\s+)?"
    r"[^.!?\n]{0,50}\bjournal,?\s+not\s+"
    r"(?:ICLR|ICRL|NeurIPS|ICML|ACL|AAAI|CVPR)\b|"
    r"\b(?:that\s+said,\s+)?i\s+may\s+not\s+be\s+fully\s+aware\b"
    r"[^.!?\n]{0,140}\bdefer\s+to\s+(?:the\s+)?"
    r"(?:area\s+and\s+meta\s+chairs|"
    r"area\s+chairs?|meta\s+chairs?)\b[^.!?\n]{0,60}\bvenue\s+fit\b|"
    r"\b(?:but\s+)?if\s+they\s+resubmit\s+(?:this|the)\s+paper\s+to\s+"
    r"another\s+venue,?|"
    r"\b(?:i|we)\s+believe\s+(?:a\s+)?speciali[sz]ed\s+"
    r"[^.!?\n]{0,60}\b"
    r"(?:conferences?|journals?)\b[^.!?\n]{0,50}\b"
    r"(?:are|is|would\s+be)\s+(?:an?\s+)?better\s+(?:venue|fit)\b|"
    r"\band\s+resubmitting\b[^.!?\n]{0,180}\b"
    r"new\s+convincing\s+evaluations\s+are\s+provided\b|"
    r"\b(?:i|we)\s+would\s+submit\s+(?:this|the)\s+work\s+to\s+"
    r"[^.!?\n]{0,80}\bconferences?\b"
    r"(?:\s+including\s+biometrics\s+\(e\.g\.,\s*FG2025\))?|"
    r"\b(?:this|the)\s+paper\s+would\s+be\s+more\s+appropriately\s+"
    r"published\s+as\s+(?:an?\s+)?(?:blog\s+post|opinion\s+piece)\b"
    r"[^.!?\n]{0,100}\b(?:peer\s+review|ICLR)\b|"
    r"\b(?:i|we)\b[^.!?\n]{0,80}\b(?:like|happy)\s+to\s+see\s+"
    r"(?:it|this|(?:this|the)\s+(?:paper|work))\s+published\s+"
    r"(?:at|in)\s+(?:an?\s+|the\s+)?(?:top\s+)?(?:venue|conference)\b|"
    r"\b(?:the\s+)?findings\s+would\s+be\s+of\s+more\s+interest\s+for\s+"
    r"audiences?\s+of\s+[^.!?\n]{0,80}\bjournals?\b[^.!?\n]{0,120}\b"
    r"rather\s+than\s+(?:an?\s+)?AI\s+conference\b|"
    r"\b(?:i|we)\s+would\s+recommend\s+that\s+the\s+authors\b"
    r"[^.!?\n]{0,100}\bresubmit\s+to\s+other\s+venues?\b|"
    r"\b(?:the\s+)?reviewer\s+thinks\s+it\s+is\s+not\s+enough\s+for\s+"
    r"(?:an?\s+)?publication\s+at\s+(?:an?\s+)?top[-\s]tier\s+"
    r"conference\s+like\s+(?:ICLR|NeurIPS|ICML|ACL|AAAI|CVPR)\b|"
    r"\b(?:more\s+)?speciali[sz]ed\s+journals?\b[^.!?\n]{0,120}\b"
    r"would\s+be\s+(?:an?\s+)?better\s+fit\b|"
    r"\bbut\s+less\s+ideal\s+for\s+(?:an?\s+)?"
    r"(?:ICLR|NeurIPS|ICML|ACL|AAAI|CVPR)\s+paper\b|"
    r"\bbefore\s+resubmitting\s+to\s+(?:an?\s+)?future\s+venue\b|"
    r"\b(?:may\s+be|maybe)\s+(?:an?\s+)?more\s+speciali[sz]ed\s+"
    r"QML\s+venue\s+is\s+more\s+ideal\b|"
    r"\b(?:i|we)\s+leave\s+the\s+decision\s+for\s+(?:the\s+)?"
    r"AC\s+and\s+other\s+reviewers\b[^.!?\n]{0,140}\b"
    r"conference\s+like\s+(?:ICLR|NeurIPS|ICML|ACL|AAAI|CVPR)\b|"
    r"\bmy\s+[\"'“”]?reject[\"'“”]?\s+recommendation\b"
    r"[^.!?\n]{0,260}\bsuitability\s+of\s+(?:this|the)\s+paper\s+for\s+"
    r"(?:ICLR|NeurIPS|ICML|ACL|AAAI|CVPR)\s+as\s+a\s+whole\b|"
    r"\b(?:it\s+seems\s+to\s+me\s+)?(?:this|the)\s+paper\s+would\s+get\s+"
    r"(?:an?\s+)?more\s+meaningful\s+review\b[^.!?\n]{0,140}\b"
    r"venue\s+like\s+(?:SIGGRAPH(?:\s+or\s+CVPR)?|CVPR|ICLR|NeurIPS|"
    r"ICML|ACL|AAAI)\b|"
    r"\bas\s+such,?\s+it\s+may(?:be|\s+be)\s+worth\s+questioning\s+the\s+"
    r"adequacy\s+of\s+(?:ICLR|NeurIPS|ICML|ACL|AAAI|CVPR)\s+as\s+a\s+"
    r"venue\s+to\s+publish\s+(?:this|the)\s+paper\b,?|"
    r"\bfits\s+better\s+within\s+the\s+scope\s+of\s+"
    r"[^.!?\n]{0,100}\bthan\s+(?:an?\s+)?ML\s+research\s+venue\b|"
    r"\bbut\s+not\s+for\s+the\s+"
    r"(?:ICLR|NeurIPS|ICML|ACL|AAAI|CVPR)\s+audience\b|"
    r"\b(?:i|we)\s+(?:do\s+not|don't)\s+know\s+if\s+"
    r"(?:ICLR|NeurIPS|ICML|ACL|AAAI|CVPR)\s+is\s+appropriate\s+for\s+"
    r"this\s+kind\s+of\s+paper\b|"
    r"\bwhy\s+did\s+you\s+choose\s+"
    r"(?:ICLR|ICRL|NeurIPS|ICML|ACL|AAAI|CVPR)\s+as\s+a\s+venue\b"
    r"(?:\s+for\s+this\s+work)?|"
    r"\boverall,?\s+(?:i|we)\s+(?:am|are)\s+not\s+sure\s+whether\s+"
    r"it\s+is\s+the\s+best\s+venue\b|"
    r"\bperhaps\s+the\s+machine\s+learning\s+community\s+in\s+"
    r"(?:ICLR|NeurIPS|ICML)\b[^.!?\n]{0,160}\b"
    r"best\s+audiences?\s+for\s+this\s+work\b[^.!?\n]{0,100}\b"
    r"not\s+a\s+typical\s+paper\b|"
    r"\b(?:it|this|the\s+paper|the\s+work)\s+might\s+be\s+better\s+"
    r"positioned\s+for\s+a\s+venue\b[^.!?\n]{0,180}\b"
    r"(?:EMNLP|ACL|KDD|WWW|SIGIR|AAAI|CVPR)\b[^.!?\n]{0,180}|"
    r"\bbefore\s+resubmission\s+to\s+(?:an?\s+)?major\s+venue\b|"
    r"\b(?:this|the)\s+contribution\s+would\s+be\s+better\s+appreciated\s+"
    r"in\s+(?:an?\s+)?[\"'“”]?datasets\s+and\s+benchmarks[\"'“”]?\s+track\b"
    r"[^.!?\n]{0,160}\b(?:ICLR|NeurIPS|ICML)\b[^.!?\n]{0,80}\btrack\b|"
    r"\b(?:i|we)\s+(?:am|are)\s+not\s+sure\s+whether\s+it\s+fits\s+"
    r"better\s+under\s+(?:an?\s+)?benchmark\s+or\s+dataset\s+track\b"
    r"[^.!?\n]{0,80}|"
    r"\bto\s+make\s+this\s+work\s+more\s+relevant\s+for\s+"
    r"(?:the\s+)?(?:ICLR|NeurIPS|ICML|ACL|AAAI|CVPR)\s+venue\b"
    r"[^.!?\n]{0,220}|"
    r"\b(?:its|this\s+paper['’]s|the\s+paper['’]s)\s+relevance\s+to\s+"
    r"(?:an?\s+)?ML\s+venue\s+is\s+questionable\b|"
    r"\bwhich\s+aligns\s+better\s+with\s+NLP\s+venues\b[^.!?\n]{0,160}|"
    r"\bthen\s+(?:i|we)\s+(?:do\s+not|don't)\s+think\s+that\s+"
    r"(?:ICLR|NeurIPS|ICML|ACL|AAAI|CVPR)\s+is\s+the\s+ideal\s+venue\s+"
    r"for\s+(?:this|the)\s+paper\b|"
    r"\b(?:i|we)\s+find\s+this\s+to\s+be\s+more\s+of\s+(?:an?\s+)?"
    r"workshop\s+contribution\b|"
    r"\b(?:i|we)\s+(?:do\s+not|don't)\s+think\s+"
    r"(?:ICLR|NeurIPS|ICML|ACL|AAAI|CVPR)\s+is\s+the\s+ideal\s+place\s+"
    r"to\s+publish\s+this\s+kind\s+of\s+result\b|"
    r"\b(?:this|the)\s+work\s+seems\s+better\s+suited\s+as\s+"
    r"(?:an?\s+)?report\s+or\s+blog\s+post,?\s+rather\s+than\s+"
    r"(?:an?\s+)?distinct\s+contribution\s+to\s+academic\s+research\b|"
    r"\benough\s+novelty\s+and\s+insight\s+to\s+be\s+presented\s+at\s+"
    r"this\s+conference\b|"
    r"\b(?:this|the)\s+paper\s+appears\s+to\s+be\s+more\s+closely\s+"
    r"aligned\s+with\s+AI\s+applications\s+in\s+education\s+venues\b"
    r"[^.!?\n]{0,180}\bonce\s+it\s+has\s+been\s+improved\b|"
    r"\bthan\s+(?:an?\s+)?paper\s+accepted\s+by\s+(?:an?\s+)?"
    r"conference\b|"
    r"\bjustifies\s+acceptance\s+at\s+"
    r"(?:ICLR|NeurIPS|ICML|ACL|AAAI|CVPR)\b[^.!?\n]{0,220}|"
    r"\bmore\s+suitable\s+for\s+(?:an?\s+)?opinion\s+piece\b|"
    r"\bbut\s+does\s+not\s+yet\s+rise\s+to\s+the\s+level\s+of\s+being\s+"
    r"accepted\s+to\s+(?:an?\s+)?conference\b|"
    r"\b(?:i|we)\s+think\s+"
    r"(?:ICLR|NeurIPS|ICML|ACL|AAAI|CVPR)\s+should\s+accept\s+such\s+papers\b"
    r"[^.!?\n]{0,120}|"
    r"\band\s+it\s+should\s+be\s+accepted\s+to\s+"
    r"(?:ICLR|NeurIPS|ICML|ACL|AAAI|CVPR)\b|"
    r"\b(?:this|the)\s+paper\s+was\s+previously\s+submitted\s+to\s+"
    r"(?:ICLR|NeurIPS|ICML|ACL|AAAI|CVPR)(?:\s+\d{4})?\s+and\s+rejected\b"
    r"[^.!?\n]{0,140}|"
    r"\bnot\s+proper\s+for\s+(?:ICLR|NeurIPS|ICML|ACL|AAAI|CVPR)\b|"
    r"\b(?:this|the)\s+work\s+is\s+not\s+sufficient\s+to\s+justify\s+"
    r"acceptance\s+at\s+(?:an?\s+)?venue\s+like\s+"
    r"(?:ICLR|NeurIPS|ICML|ACL|AAAI|CVPR)\b|"
    r"\bif\s+other\s+reviewers\s+read\s+(?:this|the)\s+paper\b"
    r"[^.!?\n]{0,220}\bfine\s+with\s+acceptance\b|"
    r"\baccording\s+to\s+(?:ICLR|NeurIPS|ICML|ACL|AAAI|CVPR)\s+policy,?\s+"
    r"(?:this|the)\s+paper\s+should\s+be\s+desk[-\s]rejected\b|"
    r"\bthese\s+papers\s+do\s+often\s+get\s+accepted\s+into\s+"
    r"(?:ICLR|NeurIPS|ICML|ACL|AAAI|CVPR)\s+and\s+this\s+contribution\s+"
    r"(?:i|we)\s+would\s+deem\s+to\s+be\s+highly\s+relevant\b|"
    r"\band\s+should\s+be\s+desk[-\s]rejected\b|"
    r"\bthe\s+overall\s+contribution\s+of\s+(?:this|the)\s+paper\s+"
    r"does\s+not\s+reach\s+the\s+bar\s+of\s+acceptance\s+of\s+"
    r"(?:this|the)\s+top[-\s]tier\s+conference\b|"
    r"\bmaking\s+me\s+hard\s+to\s+accept\s+it\s+to\s+"
    r"(?:ICLR|NeurIPS|ICML|ACL|AAAI|CVPR)\b|"
    r"\b(?:i|we)\s+just\s+wonder\s+if\s+(?:an?\s+)?better\s+audience\s+"
    r"for\s+[^.!?\n]{0,100}\bmight\s+be\s+found\s+at\s+(?:say\s+)?"
    r"[^.!?\n]{0,80}\brather\s+than\s+"
    r"(?:ICLR|NeurIPS|ICML|ACL|AAAI|CVPR)\b|"
    r"\bthat\s+make\s+it\s+unsuitable\s+for\s+acceptance\s+at\s+"
    r"this\s+conference\b|"
    r"\b(?:i|we)\s+tried\s+my\s+best\s+to\s+review\s+(?:this|the)\s+"
    r"paper\s+from\s+the\s+viewpoint\s+of\s+"
    r"(?:ICLR|NeurIPS|ICML|ACL|AAAI|CVPR)\b|"
    r"\b(?:could|can)\s+you\s+better\s+explain\s+how\s+this\s+fits\s+"
    r"into\s+(?:ICLR|NeurIPS|ICML|ACL|AAAI|CVPR)\b|"
    r"\b(?:i|we)['’]?d\s+like\s+to\s+see\s+the\s+core\s+idea\s+"
    r"published\s+in\s+the\s+conference\b|"
    r"\b(?:i|we)\s+believe\s+it\s+is\s+worth\s+being\s+accepted\s+to\s+"
    r"(?:ICLR|NeurIPS|ICML|ACL|AAAI|CVPR)\b|"
    r"\bthe\s+contributions\s+may\s+not\s+yet\s+be\s+sufficient\s+for\s+"
    r"acceptance\s+at\s+(?:an?\s+)?top[-\s]tier\s+ML\s+conference\b|"
    r"\bbefore\s+it\s+can\s+be\s+considered\s+for\s+acceptance\s+at\s+"
    r"the\s+(?:ICLR|NeurIPS|ICML|ACL|AAAI|CVPR)(?:\s+\d{4})?\s+"
    r"conference\b|"
    r"\b(?:i|we)\s+think\s+the\s+current\s+version\s+of\s+the\s+paper\s+"
    r"has\s+reached\s+the\s+acceptance\s+bar\s+of\s+"
    r"(?:ICLR|NeurIPS|ICML|ACL|AAAI|CVPR)\b|"
    r"\bgiven\s+that\s+my\s+concerns\b[^.!?\n]{0,260}\b"
    r"relevance\s+to\s+(?:ICLR|NeurIPS|ICML|ACL|AAAI|CVPR)\s+specifically\b"
    r"[^.!?\n]{0,180}|"
    r"\bsince\s+the\s+(?:ICLR|NeurIPS|ICML|ACL|AAAI|CVPR)\s+version,?|"
    r"\bfor\s+(?:ICLR|NeurIPS|ICML|ACL|AAAI|CVPR)\s+but\s+with\s+"
    r"significant\s+additional\s+work\b[^.!?\n]{0,100}\bsolid\s+accept\b|"
    r"\bmy\s+opinion\s+regarding\s+acceptance\s+at\s+"
    r"(?:ICLR|NeurIPS|ICML|ACL|AAAI|CVPR)\s+is\s+on\s+the\s+negative\s+side\b|"
    r"\b(?:this|it)\s+seems\s+mostly\s+(?:an?\s+)?dealbreaker\s+for\s+"
    r"acceptance\s+to\s+(?:ICLR|NeurIPS|ICML|ACL|AAAI|CVPR)\b|"
    r"\bfor\s+it\s+to\s+be\s+accepted\s+at\s+the\s+conference\s+at\s+"
    r"this\s+time\b|"
    r"\b(?:this|the)\s+paper\s+is\s+borderline\s+regarding\s+acceptance\s+"
    r"to\s+(?:ICLR|NeurIPS|ICML|ACL|AAAI|CVPR)\b|"
    r"\bmaking\s+it\s+insufficient\s+to\s+be\s+accepted\s+as\s+(?:an?\s+)?"
    r"(?:ICLR|NeurIPS|ICML|ACL|AAAI|CVPR)\s+paper\b|"
    r"\(?\b(?:this|the)\s+paper\s+was\s+previously\s+submitted\s+to\s+"
    r"(?:ICLR|NeurIPS|ICML|ACL|AAAI|CVPR),?\s+but(?=\s+)|"
    r"\b(?:i|we)\s+would\s+encourage\s+the\s+ethics\s+committee\b"
    r"[^.!?\n]{0,360}\bprevent\s+works\s+like\s+this\s+from\s+being\s+"
    r"published\s+in\s+top\s+conferences\b[^.!?\n]{0,180}|"
    r"\(?\bnot\s+published\s+at\s+(?:an?\s+)?conference,?\s+so\s+"
    r"wouldn['’]t\s+change\s+my\s+opinion\s+of\s+(?:this|the)\s+paper\b\)?|"
    r"\bbut\s+find\s+it\s+to\s+be\s+(?:a\s+bit\s+)?too\s+niche\s+for\s+"
    r"(?:ICLR|NeurIPS|ICML|ACL|AAAI|CVPR)\b|"
    r"\b(?:the\s+)?(?:ICLR|NeurIPS|ICML|ACL|AAAI|CVPR)\s+audience\s+"
    r"would\s+find\s+other\s+contributions\s+more\s+relevant\b"
    r"[^.!?\n]{0,120}|"
    r"\band\s+will\s+thus\s+prove\s+of\s+interest\s+to\s+the\s+"
    r"(?:ICLR|NeurIPS|ICML|ACL|AAAI|CVPR)\s+community\b|"
    r"\boverall,?\s+this\s+is\s+(?:a\s+)?paper\s+that\s+could\s+be\s+of\s+"
    r"relevance\s+to\s+the\s+(?:ICLR|NeurIPS|ICML|ACL|AAAI|CVPR)\s+"
    r"community\b|"
    r"\bwhether\s+(?:this|the)\s+resource\s+rises\s+to\s+the\s+level\s+of\s+"
    r"(?:an?\s+)?research\s+contribution\s+and\s+therefore\s+is\s+worthy\s+"
    r"of\s+(?:an?\s+)?conference\s+publication\b|"
    r"\bits\s+relevance\s+to\s+(?:ICLR|NeurIPS|ICML|ACL|AAAI|CVPR)\s+"
    r"may\s+therefore\s+be\s+(?:a\s+)?question\b|"
    r"\b(?:a\s+)?dedicated\s+AI\s+safety\s+conference/workshop\s+may\s+"
    r"also\s+be\s+(?:a\s+)?good\s+fit\b|"
    r"\b(?:this|the)\s+work\s+could\s+be\s+more\s+relevant\s+to\s+"
    r"(?:the\s+)?"
    r"(?:ICLR|NeurIPS|ICML|ACL|AAAI|CVPR)\s+community\b|"
    r"\bthe\s+relevance\s+of\s+(?:this|the)\s+paper\s+to\s+the\s+"
    r"(?:ICLR|NeurIPS|ICML|ACL|AAAI|CVPR)\s+community\b|"
    r"\bto\s+be\s+reasonably\s+considered\s+for\s+"
    r"(?:ICLR|NeurIPS|ICML|ACL|AAAI|CVPR)\b"
    r")"
)
_HISTORICAL_DECISION_FRAGMENT_RE = re.compile(
    r"(?i)(?:"
    r"\breceived\s+(?:an?\s+)?(?:low|high|positive|negative|borderline)\s+"
    r"rating\b|"
    r"\b(?:i|we)\s+(?:currently\s+|temporarily\s+)?rate\s+"
    r"(?:(?:this|the)\s+paper|it)\s+as\s+(?:an?\s+)?"
    r"(?:accept|reject|acceptance|rejection)\b(?:\s+temporarily)?|"
    r"\b(?:i\s+am\s+(?:assign(?:ing)?|assinging)|"
    r"the\s+reviewer\s+assigns?)\s+"
    r"(?:this|the)\s+paper\s+(?:an?\s+)?recommendation\s+of\s+"
    r"\*{0,2}\d+(?:\.\d+)?\s*:\s*"
    r"(?:accept|reject|acceptance|rejection)\b\*{0,2}|"
    r"\b(?:i|we|this\s+reviewer|the\s+reviewer)\b[^.!?\n]{0,45}\b"
    r"(?:happy|willing|prepared)\s+to\s+"
    r"(?:increase|raise|adjust|change)\s+(?:my\s+|the\s+|their\s+)?"
    r"(?:scores?|ratings?)\b|"
    r"\b(?:this|the)\s+reviewer\s+would\s+consider\s+"
    r"(?:increasing|raising|adjusting|changing)\s+(?:the\s+|their\s+)?"
    r"(?:scores?|ratings?)\b|"
    r"\b(?:this|the)\s+reviewer\s+would\s+be\s+happy\s+to\s+consider\s+"
    r"(?:increasing|raising|adjusting|changing)\s+(?:the\s+|their\s+)?"
    r"(?:scores?|ratings?)\b|"
    r"\bto\s+(?:increase|raise|adjust|change)\s+(?:my\s+|the\s+)?"
    r"(?:scores?|ratings?)\b|"
    r"\bdo\s+not\s+affect\s+(?:my\s+|the\s+)?final\s+rating\b|"
    r"\b(?:i|we)\s+(?:am|are)\s+hesitant\s+about\s+(?:my\s+|the\s+)?"
    r"rating\b(?:\s+primarily)?|"
    r"\b(?:i|we)\s+do\s+not\s+think\s+my\s+evaluation\s+and\s+rating\b"
    r"[^.!?\n]{0,40}\bchange\b|"
    r"\bpush\s+(?:(?:this|the)\s+work|my\s+opinion)\s+into\s+"
    r"(?:an?\s+|the\s+)?accept\s+range\b|"
    r"\b(?:this|the)\s+paper\b[^.!?\n]{0,40}\b"
    r"(?:is|was)\s+not\s+ready\s+for\s+publication\b|"
    r"\b(?:i|we)\s+(?:currently\s+)?vote\s+for\s+"
    r"(?:(?:weak|strong|borderline|marginal)\s+)?"
    r"(?:accept|reject|acceptance|rejection)\b|"
    r"\bbefore\s+(?:i|we)\b[^.!?\n]{0,80}\b"
    r"(?:[\"'“”]?(?:accept|reject)[\"'“”]?\s+review|"
    r"vot(?:e|ing)\s+for\s+(?:acceptance|rejection))\b|"
    r"\b(?:i|we)\s+(?:am\s+)?(?:slightly\s+)?(?:lean|leans|leaning|learning|"
    r"tend|tends|tending)\s+(?:toward|towards|to)\s+"
    r"(?:(?:weak|strong|borderline|marginal)\s+)?"
    r"(?:accept|reject|accepting|rejecting|acceptance|rejection)"
    r"(?:\s+(?:this|the)\s+paper)?(?:\s+rating)?\b|"
    r"\b(?:my\s+current\s+assessment\s+of\s+(?:this|the)\s+paper\s+is\s+|"
    r"(?:this|the)\s+paper\s+is\s+(?:in\s+the\s+)?)"
    r"(?:a\s+)?(?:weak|strong|borderline|marginal)\s+(?:for\s+)?"
    r"(?:accept|reject|acceptance|rejection)\b|"
    r"\b(?:makes?|making)\s+me\s+lean\s+(?:to|toward|towards)\s+"
    r"(?:accept|reject|acceptance|rejection)\b|"
    r"\b(?:i|we)\s+(?:am|are)\s+starting\s+(?:out\s+)?with\s+"
    r"(?:an?\s+)?(?:accept|reject)\b|"
    r"\b(?:i['’]?m|we['’]?re|i\s+am|we\s+are)\s+rejecting\s+"
    r"(?:this|the)\s+paper\b|"
    r"\b(?:i|we)\s+(?:recommend|suggest)\s+"
    r"(?:the\s+paper\s+be\s+)?(?:rejecting|rejection|accepting|acceptance)\b"
    r"(?:\s+(?:this|the)\s+paper)?|"
    r"\b(?:this|the|it)\b[^.!?\n]{0,50}\bshould\s+be\s+"
    r"(?:accepted|rejected)\b|"
    r"\b(?:i|we)\s+(?:am|are)\s+(?:ok|okay)\s+with\s+"
    r"(?:acceptance|rejection)\b|"
    r"\bclearly\s+deserving\s+(?:of\s+)?(?:acceptance|rejection)\b|"
    r"\b(?:this|it)\s+seems\s+mostly\s+(?:an?\s+)?dealbreaker\s+for\s+"
    r"(?:acceptance|rejection)(?:\s+to\s+"
    r"(?:ICLR|NeurIPS|ICML|ACL|AAAI|CVPR))?\b|"
    r"\b(?:i|we)\s+(?:am|are)\s+open\s+to\s+the\s+idea\s+of\s+"
    r"(?:accepting|rejecting)\s+(?:this|the)\s+work\b|"
    r"\bprevents?\s+me\s+from\s+giving\s+(?:an?\s+)?score\s+of\s+"
    r"(?:acceptance|rejection)\b|"
    r"\b(?:i|we)\s+(?:am|are)\s+clearly\s+trying\s+to\s+"
    r"(?:accept|reject)\s+(?:your|this|the)\s+paper\b|"
    r"\brecommendation\s*:\s*"
    r"(?:(?:weak|strong|borderline|marginal)\s+)?(?:accept|reject)\b|"
    r"\b(?:acceptance|rejection)\s+should\s+be\s+contingent\s+on\b|"
    r"\b(?:critical|important)\s+for\s+(?:this|the)\s+paper['’]s\s+"
    r"(?:acceptance|rejection)\b|"
    r"\b(?:strong|clear)\s+signal\s+for\s+(?:acceptance|rejection)\b|"
    r"\bblock(?:s|ed|ing)?\s+(?:acceptance|publication)\b|"
    r"\b(?:does|do|did)\s+not\s+(?:meet|comply\s+with)\s+(?:the\s+)?"
    r"(?:paper\s+)?publication\s+standard(?:s)?(?:\s+in\s+academia)?\b|"
    r"\b(?:i|we)\s+(?:do\s+not|don['’]t)\s+think\s+it\s+complies\s+with\s+"
    r"(?:the\s+)?(?:paper\s+)?publication\s+standard(?:s)?"
    r"(?:\s+in\s+academia)?\b|"
    r"\b(?:maybe\s+can|should|must|could)\s+desk[-\s]reject\s+it\b"
    r"[^.!?\n]{0,100}\bsubmission\s+policy\b|"
    r"\b(?:is|are|remains?|remain)\s+(?:clearly\s+|already\s+)?"
    r"(?:not\s+|in)?sufficient\s+to\s+(?:support|justify|warrant|merit)\s+"
    r"(?:its\s+|an?\s+)?(?:acceptance|publication)\b"
    r"[^.!?\n]{0,100}\b(?:conference|venue|journal)s?\b|"
    r"\b(?:is|are|was|were)\s+not\s+strong\s+enough\s+for\s+publication\b"
    r"[^.!?\n]{0,100}\b(?:conference|venue|journal)s?\b|"
    r"\b(?:i|we)\s+(?:do\s+not|don't)\s+think\s+(?:the\s+)?results?\s+"
    r"(?:is|are)\s+strong\s+enough\s+for\s+publication\b"
    r"[^.!?\n]{0,100}\b(?:conference|venue|journal)s?\b|"
    r"\b(?:does|do|did)\s+not\s+meet\s+(?:the\s+)?"
    r"(?:bar|standard|threshold)\s+(?:required\s+)?for\s+"
    r"(?:acceptance|publication)\b|"
    r"\b(?:is|are|was|were|falls?|fell)\s+(?:obviously\s+|slightly\s+)?"
    r"(?:below|short\s+of)\s+(?:the\s+)?(?:bar|standard|threshold)\s+for\s+"
    r"(?:acceptance|publication)\b|"
    r"\bbefore\s+(?:this|the)\s+paper\s+meets\s+standards?\s+for\s+"
    r"publication\b[^.!?\n]{0,100}\b(?:venue|conference|journal)\b|"
    r"\b(?:makes?|made)\s+(?:an?\s+)?good\s+case\s+for\s+publication\b"
    r"(?:\s+at\s+(?:ICLR|NeurIPS|ICML|ACL|AAAI|CVPR))?|"
    r"\bto\s+(?:justify|warrant|merit|support)\s+"
    r"(?:an?\s+|its\s+)?(?:acceptance|publication)\b"
    r"(?!\s+of\s+(?:data|code|results?|artifacts?))|"
    r"\b(?:is|are|was|were)\s+sufficient\s+to\s+support\s+"
    r"(?:an?\s+)?individual\s+publication\b|"
    r"\b(?:is|are|was|were)\s+sufficient\s+for\s+(?:an?\s+)?publication\s+"
    r"(?:in|at)\s+(?:ICLR|NeurIPS|ICML|ACL|AAAI|CVPR)\b|"
    r"\b(?:reason|grounds?)\s+for\s+(?:an?\s+)?desk[-\s]rejection\b|"
    r"\blead\s+to\s+(?:an?\s+)?desk[-\s]rejection\b|"
    r"\b(?:current\s+)?revision\s+remains\s+insufficient\s+for\s+"
    r"publication\b|"
    r"\b(?:reviewer\s+)?maintains?\s+(?:an?\s+)?negative\s+overall\s+"
    r"assessment\b|"
    r"\bbefore\s+(?:consideration\s+of\s+)?(?:acceptance|publication)\b|"
    r"\b(?:does|do|did)\s+not\s+merit\s+publication\b"
    r"[^.!?\n]{0,100}\b(?:conference|venue|journal)\b|"
    r"\bto\s+be\s+considered\s+for\s+(?:acceptance|publication)\b|"
    r"\bfalls?\s+(?:slightly\s+)?short\s+of\s+(?:the\s+)?threshold\s+for\s+"
    r"(?:acceptance|publication)\b|"
    r"\bfalls?\s+(?:slightly\s+)?short\s+of\s+"
    r"(?:acceptance|publication)\b|"
    r"\bbefore\s+being\s+published\b|"
    r"\b(?:make|makes|making)\s+(?:an?\s+)?stronger\s+case\s+for\s+"
    r"publication\b|"
    r"\bto\s+be\s+at\s+the\s+level\s+of\s+(?:acceptance|publication)\b|"
    r"\bfar\s+from\s+ready\s+to\s+publish\b|"
    r"\bfor\s+it\s+to\s+be\s+accepted\b|"
    r"\binteresting\s+enough\s+to\s+publish\b|"
    r"\bto\s+warrant\s+publication\s+at\s+(?:an?\s+)?top\s+venue\b|"
    r"\b(?:results?|findings?|experiments?)\s+are\s+already\s+enough\s+for\s+"
    r"(?:an?\s+)?(?:ICLR|NeurIPS|ICML|ACL|AAAI|CVPR)\s+publication\b|"
    r"\bfor\s+(?:an?\s+)?full\s+publication\s+(?:at|in)\s+"
    r"(?:ICLR|NeurIPS|ICML|ACL|AAAI|CVPR)\b|"
    r"\b(?:is|are|was|were)\s+(?:not\s+)?(?:independently\s+)?"
    r"sufficient\s+for\s+"
    r"(?:acceptance|publication|rejecting\s+the\s+paper)\b|"
    r"\b(?:i|we)\s+(?:do\s+not|don't)\s+find\s+(?:the\s+)?"
    r"(?:contribution|results?|evidence|analysis)\s+sufficient\s+for\s+"
    r"(?:acceptance|publication)\b|"
    r"\bfar\s+from\s+(?:the\s+)?(?:acceptance|publication)\s+threshold\b|"
    r"\bbefore\s+it\s+is\s+considered\s+for\s+"
    r"(?:acceptance|publication)\b|"
    r"\b(?:limitations?|issues?|concerns?)\s+prevent\s+"
    r"(?:acceptance|publication)\b|"
    r"\bunsuitable\s+for\s+(?:acceptance|publication)\b|"
    r"\bshort\s+of\s+(?:the\s+)?(?:acceptance|publication)\s+bar\b|"
    r"\bnot\s+(?:yet\s+)?in\s+(?:a\s+)?state\s+for\s+publication\b|"
    r"\binsufficient\s+for\s+(?:an?\s+)?top[-\s]tier\s+publication\b|"
    r"\bif\s+(?:this|the)\s+paper\s+is\s+accepted\b|"
    r"\bfor\s+(?:a\s+)?publication\s+at\s+(?:this|the)\s+venue\b|"
    r"\b(?:suggest(?:s|ed|ing)?\s+(?:this|the)\s+paper\s+)?"
    r"not\s+(?:yet\s+)?(?:to\s+be\s+)?ready\s+for\s+publication"
    r"(?:\s+at\s+(?:its|the)\s+current\s+stage)?\b|"
    r",?\s*\bhow\s+does\s+this\s+work\s+meet\s+the\s+research\s+"
    r"contribution\s+standards\s+of\s+a\s+top[-\s]tier\s+ML\s+venue\s+"
    r"versus\s+being\s+more\s+appropriate\s+for\s+[^.!?\n]{0,120}\b"
    r"(?:conferences?|tracks?)\b|"
    r"\b(?:simply\s+)?unacceptable\s+for\s+publication\b|"
    r"\bto\s+render\s+(?:this|the)\s+paper\s+publishable\b|"
    r"\bin\s+(?:an?\s+)?unpublishable\s+(?:state|form|condition)\b|"
    r"\bbefore\s+it\s+can\s+be\s+published\b|"
    r"\bthat\s+prevents\s+(?:this|the)\s+paper\s+from\s+being\s+ready\s+"
    r"for\s+publication(?:\s+at\s+this\s+time)?\b|"
    r"\b(?:this|the)\s+paper\s+(?:as\s+it\s+is\s+right\s+now\s+)?"
    r"is\s+not\s+suitable\s+for\s+publication\b|"
    r"\bto\s+meet\s+(?:the\s+)?(?:expected\s+)?(?:academic|scholarly)?\s*"
    r"standards\s+of\s+a\s+top[-\s]tier\s+"
    r"(?:publication|conference|venue)\b|"
    r"\b(?:this|the)\s+paper\b[^.!?\n]{0,80}\bbelow\s+the\s+bar\s+for\s+"
    r"publication\b|"
    r"\b(?:it\s+)?may\s+not\s+be\s+appropriate\s+for\s+"
    r"(?:this|the)\s+venue\b|"
    r"\bin\s+order\s+to\s+fit\s+(?:a\s+)?venue\s+such\s+as\s+"
    r"(?:ICLR|NeurIPS|ICML|ACL|AAAI|CVPR)\b|"
    r"\bthe\s+reviewer\s+believes\s+that\s+(?:this|the)\s+paper\s+is\s+"
    r"not\s+(?:yet\s+)?ready\s+for\s+publication\b|"
    r"\beven\s+though\s+i\s+was\s+rather\s+positive\s+in\s+the\s+"
    r"first\s+place\b|"
    r"\bthis\s+is\s+not\s+suitable\s+for\s+conference\s+review\s+in\s+"
    r"it['’]?s\s+present\s+form\b|"
    r"\bto\s+be\s+suitable\s+for\s+publication,?\s*|"
    r"\bto\s+meet\s+the\s+standards\s+of\s+a\s+top[-\s]tier\s+"
    r"conference\b|"
    r"\bnot\s+fit\s+to\s+(?:this|the)\s+venue\b|"
    r",?\s*\bmaking\s+it\s+less\s+suitable\s+for\s+"
    r"(?:this|the|a\s+)?(?:ICLR|NeurIPS|ICML|ACL|AAAI|CVPR)?\s*"
    r"(?:conference|venue)?\b"
    r")"
)
_HISTORICAL_WRAPPED_DECISION_RE = re.compile(
    r"(?i)(\b(?:recommend(?:ed|ing)?|vote|voting|lean|leaning|inclined)\b"
    r"[^.!?\n]{0,80})\n+(?=\s*(?:accept(?:ance|ed|ing)?|"
    r"reject(?:ion|ed|ing)?)\b)"
)
_HISTORICAL_CURRENT_REVIEW_CONTEXT_RE = re.compile(
    r"(?:\bI\b|(?i:"
    r"\breviewer\b|"
    r"\b(?:this|the|current|submitted)\s+"
    r"(?:paper|work|manuscript|submission|version|contribution|draft|study|research|"
    r"type\s+of\s+work)\b|"
    r"\bcurrent\s+(?:draft|version|contribution|study)\b|"
    r"\b(?:paper|work|manuscript|submission)\s+"
    r"(?:is|does|should|must|would|can|could|seems|appears|feels)\b"
    r"))"
)
_HISTORICAL_TECHNICAL_DECISION_TERM_RE = re.compile(
    r"(?i)(?:"
    r"[“\"][^”\"\n]{1,400}[”\"]\s*,\s*"
    r"(?:ICLR|NeurIPS|ICML|ACL|ARR|AAAI|CVPR|ECCV)\s+\d{4}\b|"
    # Model interfaces, preference labels, sampling, and safety refusals use
    # decision vocabulary as technical terminology rather than peer-review votes.
    r"\b(?:LLMs?|models?|networks?|baselines?|systems?|encoders?|decoders?|"
    r"architectures?)\b[^.!?\n]{0,90}\baccept(?:s|ed|ing)?\b"
    r"[^.!?\n]{0,65}\binputs?\b|"
    r"\baccept(?:s|ed|ing)?\b[^.!?\n]{0,80}\bas\s+(?:an?\s+)?"
    r"input(?:s|\s+(?:prompts?|features?|conditions?))?\b|"
    r"\baccept(?:s|ed|ing)?\s+"
    r"(?:(?:additional|conditional|multiple|raw|reference|image|audio|video|"
    r"graph|subgraph|natural[-\s]language)\s+){1,6}inputs?\b|"
    r"\bchosen\b[^.!?\n]{0,45}\brejected\b"
    r"(?=[^.!?\n]{0,45}\b(?:DPO|preference|responses?|ratings?|probabilities|"
    r"pairs?|items?)\b)|"
    r"\brejected\b[^.!?\n]{0,45}\bchosen\b"
    r"(?=[^.!?\n]{0,45}\b(?:DPO|preference|responses?|ratings?|probabilities|"
    r"pairs?|items?)\b)|"
    r"\bchosen\s*[-–—/,]\s*rejected\b|"
    r"\b(?:chosen|preferred)\s+(?:and|or|versus|vs\.?|/)\s+"
    r"(?:rejected|dispreferred)\b|"
    r"\b(?:rejected|dispreferred)\s+(?:and|or|versus|vs\.?|/)\s+"
    r"(?:chosen|preferred)\b|"
    r"\b(?:label(?:s|ed|ing)?\s+[^.!?\n]{0,45}\bas\s+)?[\"'“”]?chosen[\"'“”]?"
    r"\s+(?:and|or)\s+[\"'“”]?rejected[\"'“”]?\b|"
    r"\b(?:chosen|preferred)\b[^.!?\n]{0,50}\b"
    r"(?:rejected|dispreferred)\s+"
    r"(?:responses?|answers?|rollouts?|items?|pairs?|ones?)\b|"
    r"\b(?:accepted|rejected)\s+(?:sets?|stories|examples?|samples?|labels?|"
    r"candidates?|prompts?|outputs?|events?|responses?|datapoints?|probabilities|"
    r"trajectories|sentences|items?|pairs?|tasks?|contexts?)\b|"
    r"\b(?:accepted|acceptance|accept)\s+lengths?\b|"
    r"\b(?:mean|average|expected)\s+accept\s+lengths?\b|"
    r"\b(?:drafted[-\s]token|step(?:[-\s]?wise))\s+acceptance\b|"
    r"\b(?:automaton['’]s\s+)?acceptance\s+(?:events?|structures?)\b|"
    r"\b(?:data|samples?|outputs?|responses?|probabilities|labels?|trajectories|"
    r"sentences|items?|pairs?|actions?|drafts?|tokens?|queries|solutions?)\b"
    r"[^.!?\n]{0,60}\b"
    r"(?:accepted|rejected)(?:\s*(?:,|and|or)\s*(?:accepted|rejected))?\b|"
    r"\b(?:accepted|rejected)\b[^.!?\n]{0,30}\b(?:and|or)\s+"
    r"(?:accepted|rejected|manually\s+revised)\b"
    r"(?=[^.!?\n]{0,55}\b(?:data|samples?|outputs?|responses?|proportion|DFA|"
    r"labels?|trajectories|sentences|items?|pairs?)\b)|"
    r"\bwhat\s+proportion\b[^.!?\n]{0,80}\baccepted,\s*rejected,\s*or\s+"
    r"manually\s+revised\b|"
    r"\b(?:solutions?|outputs?|strings?|traces?)\b[^.!?\n]{0,70}\b"
    r"accepted\s+by\s+(?:the\s+)?DFA\b|"
    r"\boutputs?\s+must\s+be\s+accepted\s+by\s+(?:a\s+|the\s+)?DFA\b|"
    r"\b(?:MH|MCMC|Metropolis(?:[-\s]Hastings)?)\b[^.!?\n]{0,100}\b"
    r"(?:accept(?:ance|s|ed|ing)?|reject(?:ion|s|ed|ing)?)\b|"
    r"\b(?:accept(?:ance|s|ed|ing)?|reject(?:ion|s|ed|ing)?)\b"
    r"[^.!?\n]{0,100}\b(?:MH|MCMC|Metropolis(?:[-\s]Hastings)?|"
    r"Metropolis\s+criterion)\b|"
    r"\b(?:GMM|clustering|sampler|sampling|decoder)\b[^.!?\n]{0,100}\b"
    r"accept\s*[-–—/]\s*reject\b[^.!?\n]{0,65}|"
    r"\baccept\s*[-–—/]\s*reject\s+"
    r"(?:coin|mechanism|step|process|rule)\b|"
    r"\bpseudorandom\s+acceptance\b|"
    r"\bsoft\s+acceptance\b|"
    r"\b(?:rejection\s+sampling|reject\s+sampling|speculative\s+sampling)\b"
    r"[^.!?\n]{0,200}\b(?:accept|reject)(?:s|ed|ing)?\b"
    r"[^.!?\n]{0,80}\b(?:tokens?|samples?|proposals?|trajector(?:y|ies)|"
    r"events?|actions?|portion)\b|"
    r"\bactions?\s+(?:is|are|was|were)\s+accepted\b[^.!?\n]{0,90}\b"
    r"(?:binary\s+)?outcomes?\s*\([^)]*\b(?:accept|reject)\b[^)]*\)|"
    r"\bbinary\s+outcomes?\s*\([^)]*\baccept\s+or\s+reject\b[^)]*\)|"
    r"\b(?:reject|rejection)[-\s](?:based[-\s])?fine[-\s]tuning\b|"
    r"\brejection[-\s]sampled\b|"
    r"\b(?:safety\s+model|safety\s+system|guardrail|filter|router|refusal|"
    r"jailbreak|privacy[-\s]inference)\b[^.!?\n]{0,100}\b"
    r"reject(?:s|ed|ing|ion)?\b[^.!?\n]{0,55}\b"
    r"(?:queries|requests|prompts?|responses?|inputs?|attacks?)\b|"
    r"\b(?:LLMs?|models?|servers?|filters?|guardrails?|checkpoints?|systems?)\b"
    r"[^.!?\n]{0,100}\breject(?:s|ed|ing)?\b[^.!?\n]{0,75}\b"
    r"(?:instructions?|queries|requests?|assumptions?|updates?|outputs?|prompts?|"
    r"responses?)\b|"
    r"\breject(?:s|ed|ing)?\b[^.!?\n]{0,75}\b(?:instructions?|queries|requests?|"
    r"assumptions?|updates?|outputs?|prompts?|responses?)\b"
    r"(?=[^.!?\n]{0,80}\b(?:LLMs?|models?|servers?|filters?|guardrails?|"
    r"checkpoints?|systems?)\b)|"
    r"\bverifiers?\b[^.!?\n]{0,70}\breject(?:s|ed|ing)?\s+"
    r"(?:all|every)\s+actions?\b|"
    r"\brejection[-‐-―\s]oriented\b|"
    r"\binduc(?:e|es|ed|ing)\s+(?:the\s+)?model\s+rejection\b|"
    r"\breject(?:s|ed|ing)?\b[^.!?\n]{0,50}\b"
    r"(?:malicious|harmful|inappropriate|privacy[-\s]inference|jailbreak)\s+"
    r"(?:queries|requests|prompts?|responses?|inputs?|attacks?)\b|"
    r"\breject(?:s|ed|ing)?\s+(?:the\s+)?(?:original\s+)?malicious\s+"
    r"quer(?:y|ies)\b|"
    r"\breject(?:s|ed|ing)?\s+(?:the\s+)?inference\s+requests?\b|"
    r"\brejected\s+incentive\s+suffix(?:es)?\b|"
    r"\brejecting\s+privacy[-\s]inference\s+attempts?\b|"
    r"\baccept(?:s|ed|ing)?\s+(?:the\s+)?authors?(?:['’]s|['’])\s+"
    r"(?:new\s+)?(?:[\"'“”][^\"'“”\n]{1,80}[\"'“”]\s+)?"
    r"(?:goal|assumption|premise|definition|framing|argument|claim)\b|"
    r"\b(?:malicious|harmful|inappropriate|privacy[-\s]inference|jailbreak)\s+"
    r"(?:queries|requests|prompts?|responses?|inputs?|attacks?)\b"
    r"[^.!?\n]{0,45}\b(?:are|were|is|was|be|being)\s+rejected\b|"
    # Publishing artifacts and describing already-published work are author
    # actions or bibliographic facts, not a verdict on the current manuscript.
    r"\bpublish(?:es|ed|ing)?\s+(?:(?:their|our|its|the|this|a|an)\s+)?"
    r"(?:code|data|datasets?|artifacts?|benchmarks?|results?|repository)\b|"
    r"\bpublish(?:es|ed|ing)?\s+(?:a\s+)?(?:detailed\s+)?breakdown\s+of\s+"
    r"(?:the\s+)?dataset\b|"
    r"\b(?:previous|prior|existing|recent|recently\s+published)\s+"
    r"(?:papers?|works?|articles?|frameworks?|studies?)\b[^.!?\n]{0,55}\b"
    r"(?:published|preprinted)\b|"
    r"\brecently\s+published\s+or\s+preprinted\s+"
    r"(?:RL\s+)?(?:tool[-\s]use\s+)?frameworks?\b|"
    r"\b(?:papers?|works?|methods?|frameworks?|algorithms?)\s+published\s+"
    r"(?:on|about)\s+[^.!?\n]{1,80}\b(?:missing|omitted|cited|compared|"
    r"discussed|included|reviewed)\b[^.!?\n]*|"
    r"\b(?:this\s+)?(?:obvious\s+)?(?:idea|method|approach|algorithm|"
    r"construction)\s+has\s+been\s+published\b[^.!?\n]{0,100}\b"
    r"(?:ACL\s+Anthology|arXiv|DOI|citation|reference|https?://)\b|"
    r"\bpublished\s+(?:counterparts?|experiments?)\b|"
    r"\b(?:previous|prior|existing|recent)\s+(?:papers?|works?|articles?|"
    r"frameworks?|studies?)\s+published\s+(?:on|about|in|at)\b|"
    r"\b(?:papers?|works?|articles?|frameworks?|studies?)\s+published\s+"
    r"(?:on|about)\s+(?:this|the)\s+topic\b|"
    r"\b(?:an?\s+)?(?:\d{4}\s+)?paper\s+published\s+(?:in|at|by)\s+"
    r"[^,.!?;\n]{1,80}|"
    r"\bpublished\s+(?:case\s+reports?|frameworks?|toolkits?|corpora|"
    r"literature|benchmarks?|datasets?)\b|"
    r"\b(?:datasets?|benchmarks?|code|artifacts?)\s+(?:is|are|was|were|to\s+be)\s+"
    r"published\b(?:[^.!?\n]{0,70}\b(?:training|testing|evaluation|release)\b)?|"
    r"\b(?:recent|prior|previous)\s+work\s+published\s+at\s+"
    r"(?:this|the)\s+(?:conference|venue)\b|"
    r"\b(?:works?|papers?|baselines?|datasets?|articles?|problems?|models?)\b"
    r"[^.!?\n]{0,45}\bpublished\s+(?:in|at|on)\s+"
    r"(?:\d{4}|[A-Z][A-Za-z.-]*\s*\d{2,4})\b|"
    r"\b(?:algorithms?|methods?|systems?|prior\s+works?|previous\s+works?)\s+"
    r"(?:are|were|was|is)?\s*(?:typically\s+)?published\s+(?:in|at)\s+"
    r"(?:an?\s+)?(?:conference|journal|venue)\s+such\s+as\s+"
    r"(?-i:[A-Z][A-Z0-9-]{1,15})\b|"
    # Meta-science papers model conference outcomes as data. Keep those
    # variables intact so their methods and limitations remain reviewable.
    r"\b(?:paper|submission|conference)\s+(?:acceptance|rejection)\s+"
    r"(?:decisions?|outcomes?|statuses?|patterns?|chances?|rates?|variables?|"
    r"labels?|metrics?|probabilit(?:y|ies))\b|"
    r"\b(?:correlation|association|effect|impact|influence)\s+(?:with|on)\s+"
    r"(?:paper\s+)?(?:acceptance|rejection)\b|"
    r"\b(?:sentiment|scores?|disagreement|interaction|engagement|metadata|"
    r"arXiv\s+posting)\b[^.!?\n]{0,90}\b"
    r"(?:influenc(?:e|es|ed|ing)|affect(?:s|ed|ing)?|predict(?:s|ed|ing)?|"
    r"correlat(?:e|es|ed|ing))\b[^.!?\n]{0,55}\b"
    r"(?:paper\s+)?(?:acceptance|rejection)(?:\s+decisions?)?\b|"
    r"\b(?:accepted\s+and\s+rejected|accepted|rejected)\s+papers\b|"
    r"\b(?:additional|predicted|actual|historical)\s+(?:accepted|rejected)\s+"
    r"papers\b|"
    r"\bpapers\b[^.!?\n]{0,55}\b(?:not\s+)?(?:accepted|rejected)\b"
    r"(?=[^.!?\n]{0,60}\b(?:dataset|analysis|prediction|labels?|tiers?|metric|"
    r"model|algorithm)\b)|"
    r"\bacceptance[-‐-―\s]rate\s+analysis\b|"
    r"\b(?:higher|lower)\s+acceptance\s+through\s+Pinsker\b|"
    r"\bculling\s+or\s+acceptance\b|"
    r"\bacceptance\s+(?:tiers?|metrics?|rankings?)\b|"
    r"\b(?:reduced|increased|higher|lower)\s+acceptance\s+of\s+"
    r"(?:manipulated|generated|synthetic|sampled)\s+papers\b|"
    r"\bdesk[-‐-―\s]reject(?:ion)?\s+(?:variable|feature|label|metric)\b|"
    r"\bacceptance\s+criteria\b[^.!?\n]{0,50}\b"
    r"(?:vary|varies|differ|differs|operationali[sz]ed|used)\b|"
    # A model/pipeline may itself predict or synthesize a rating.
    r"\b(?:pipeline|system|method|model|LLM|judge|annotator|classifier)\b"
    r"[^.!?\n]{0,100}\b(?:initial|integer|ordinal|quality|response|output)?\s*"
    r"(?:score|rating)s?\b[^.!?\n]{0,80}\b"
    r"(?:task|synthesis|evaluation|prediction|distribution|calibration|label)s?\b|"
    # Venue ethics and an experimental competition track are technical context.
    r"\b(?:paper|work|method)\s+aligns?\s+with\s+"
    r"(?:ICLR|NeurIPS|ICML|ACL|ARR|AAAI|CVPR)['’]s\s+ethical\s+standards?\b|"
    r"\brandom\s+track\s+from\s+(?:the\s+)?\d{4}\s+SAT\s+competition\b"
    r"[^.!?\n]{0,80}\bbetter\s+suited\b|"
    r"\b(?:sequential\s+|batched\s+)?rejection[-\s]"
    r"(?:sampling|sampler|mechanism|method|region)(?:[-\s]based)?\b|"
    r"\bacceptance[-–—/]rejection(?:\s+(?:sampling|sampler))?\b|"
    r"\baccept[-–—/]reject\s+"
    r"(?:MH\s+steps?|association|sampling|sampler|signals?)\b|"
    r"\baccept-if-correct\b|"
    r"\b(?:outlier|noise|disturbance|proposal)\s+rejection\b|"
    r"\b(?:over|under)[-\s]rejection\b[^.!?\n]{0,30}\b"
    r"(?:drafts?|samples?|tokens?|proposals?)\b|"
    r"\b(?:acceptance|rejection)\s+"
    r"(?:probability|rate|ratio|region|test|length|criterion|sampling|"
    r"mechanism)\b|"
    r"\b(?:raise|raises|raising|increase|increases|increasing|improve|improves|"
    r"improving)\s+acceptance\b|"
    r"\b(?:perceived|expected|growing|increasing|broad)\s+acceptance\b|"
    r"\bacceptance\s+(?:depends|depending)\b|"
    r"\bacceptance\s+of\s+(?:edits?|changes?|proposals?|reactions?|tokens?|drafts?)\b|"
    r"\b(?:recommend|recommends|recommended|recommending)\b[^.!?\n]{0,50}\b"
    r"accept(?:s|ed|ing)?\s+(?:the|an?|this)\s+"
    r"(?:(?:one[-\s]time|fixed|offline)\s+)?"
    r"(?:cost|claim|argument|assumption|definition|interpretation|hypothesis|"
    r"input|token|sample|proposal|connection|conclusion|trade[-\s]?off)\b|"
    r"\baccepted[-\s](?:span\s+)?length\b|"
    r"\b(?:generally|commonly|broadly|widely|universally)\s+accepted\b|"
    r"\bupon\s+(?:acceptance|publication|deanonymization)\b|"
    r"\bpublishable\s+(?:presentation|video|artifact|output)\b|"
    r"\bpublication[-\s]ready\s+"
    r"(?:illustrations?|figures?|tables?|artifacts?|outputs?)\b|"
    r"\bwithout\s+publication\b|"
    r"\bpublication\s+of\s+(?:the\s+)?"
    r"(?:data|dataset|code|artifact|results?|attack\s+data)\b|"
    r"\b(?:analysis|analy[sz](?:e|es|ed|ing)|study|model|prediction)\b"
    r"[^.!?\n]{0,80}\b"
    r"(?:a|the)\s+paper\s+being\s+accepted\s+(?:at|to|in)\b|"
    r"\bprior\s+work\b[^.!?\n]{0,80}\baccepted\b|"
    r"\b(?:archival\s+paper|workshop\s+paper|prior\s+paper|previous\s+paper|"
    r"widely\s+used[^.!?\n]{0,40}\b(?:method|approach|baseline)|"
    r"following\s+archival\s+paper)\b"
    r"[^.!?\n]{0,100}\baccepted\s+(?:at|to|in)\s+"
    r"(?:ICLR|NeurIPS|ICML|ACL|ARR|AAAI|CVPR|MICCAI)"
    r"(?:\s*[- ]?\d{2,4})?\b|"
    r"\bpaper\s+titled\b[^.!?\n]{0,180}\baccepted\s+(?:at|to|in)\s+"
    r"(?:ICLR|NeurIPS|ICML|ACL|ARR|AAAI|CVPR|MICCAI)\b|"
    r"\b(?:papers?|submissions?)\b[^.!?\n]{0,80}\b"
    r"(?:get|gets|are|were|may\s+get)\s+desk[-\s]rejected\b|"
    r"\b(?:number|total\s+number)\s+of\s+desk[-\s]rejected\s+papers?\b|"
    r"\bdesk[-\s]accepted\b|"
    r"\bdesk[-\s]rejection\s+(?:ILP|mechanisms?|policies?|process|step|"
    r"problem|optimization|optimisation)\b|"
    r"\b(?:accept|accepts|accepted|accepting)\s+"
    r"(?:(?:this|the|an?)\s+)?(?:current\s+)?"
    r"(?:input|token|sample|proposal|hypothesis|argument|request|connection|"
    r"claim|conclusion|interpretation|limitation|benchmark|practice|definition|"
    r"notation|method|assumption|theorem|lemma|proof|bound|dependence|value|"
    r"result|evidence|accuracy|performance|trade[-\s]?off)\b|"
    r"\baccept(?:s|ed|ing)?\s+that\b|"
    r"\b(?:reject|rejects|rejected|rejecting)\s+"
    r"(?:the\s+|an?\s+)?(?:valid\s+|invalid\s+|harmful\s+|benign\s+|unsafe\s+)?"
    r"(?:input|token|sample|proposal|hypothesis|null|outlier|noise|response|"
    r"output|generation|example|trajectory|claim|conclusion|interpretation|reaction|"
    r"effects?|misbehavio(?:u)?r|actions?)\b|"
    r"\b(?:token|sample|proposal|generation|draft)\s+"
    r"(?:rejection(?:\s+and\s+acceptance)?|"
    r"acceptance(?:\s+and\s+rejection)?)\b|"
    r"\b(?:accepted|rejected)\s+by\s+(?:human\s+)?annotators?\b|"
    r"\b(?:accepted|rejected)(?:[-/]\w+)?\s+"
    r"(?:draft\s+)?(?:tokens?|samples?|proposals?|reactions?|responses?|outputs?)\b|"
    r"\b(?:tokens?|samples?|proposals?|reactions?|responses?|outputs?)\s+"
    r"(?:were|are|was|is)\s+(?:accepted|rejected)\b|"
    r"\b(?:tokens?|actions?|drafts?|queries|solutions?|steps?)\s+"
    r"(?:were|are|was|is|may\s+be|can\s+be)\s+(?:accepted|rejected)\b|"
    r"\b(?:accept|accepts|accepted|accepting|reject|rejects|rejected|rejecting)\s+"
    r"(?:all\s+|every\s+|the\s+|an?\s+)?"
    r"(?:(?:semantically|technically|apparently)\s+)?"
    r"(?:(?:correct|incorrect|valid|invalid|plausible)\s+)?"
    r"(?:tokens?|actions?|drafts?|queries|solutions?|steps?)\b|"
    r"\btokens?\s+rejected\s+by\s+(?:the\s+)?verifier\b|"
    r"\baccept\s+or\s+reject\s+using\b[^.!?\n]{0,80}|"
    r"\baccepted/rejected\b|"
    r"\bpublication\s+(?:bias|date|year|record|dataset|metadata|corpus)\b"
    r"|^\s*score\s*:\s*\d+(?:\.\d+)?\s*[\"'’]?\b"
    r"[^.!?\n]{0,140}\b(?:poison|prompt|model['’]?s?\s+internal\s+state|"
    r"output\s+distribution|final\s+scoring\s+step)\b"
    r"|\brecommendation\s+score\b[^.!?\n]{0,100}\b"
    r"(?:explanation|distribution|model|training|increase|decrease)\b"
    r"|\b(?:higher|lower)?\s*score\s+prediction\s+"
    r"(?:accuracy|error|performance)\b"
    r"|\b(?:claims?|arguments?|assumptions?|definitions?|interpretations?|"
    r"hypotheses?)\s+(?:can|could|should|would|may|might)\s+be\s+accepted\b"
    r"|\breview\s+scores?\s+(?:might|may|could|can|would)\s+"
    r"(?:follow|obey|use|be\s+model(?:l)?ed\s+(?:by|as))\b"
    r"[^.!?\n]{0,80}\b(?:model|distribution)\b"
    r"|\b(?:LLM|model)\s+generate(?:s|d|ing)?\s+(?:the\s+)?"
    r"review\s+score\b"
    r"|\breview\s+score\s+[\"'“”]?"
    r"(?:reflects?|predicts?|measures?|represents?|models?)\b"
    r"|\b(?:scalar\s+)?range\s+\[[^\]\n]{1,40}\]\s+for\s+"
    r"(?:the\s+)?review\s+score\b"
    r"|\b(?:this|the)\s+paper\s+is\s+submitted\s+to\s+the\s+"
    r"Datasets\s+and\s+Benchmarks\s+track\b"
    r"|\b(?:relatively\s+)?(?:high|low)\s+score\s+when\s+considering\s+"
    r"(?:the\s+)?benchmark['’]s\s+complexity\b"
    r"|\b(?:the\s+)?experiment\b[^.!?\n]{0,120}\b"
    r"(?:traces?|trajector(?:y|ies)|curves?|plots?)\b[^.!?\n]{0,100}\b"
    r"change\s+of\s+(?:the\s+)?score\b"
    r"|\bif\s+(?:this|the)\s+paper\s+is\s+accepted\b"
    r"(?=[^.!?\n]{0,140}\b(?:extra\s+page\s+allowance|"
    r"page\s+limit\s+(?:goes|increases|rises)\b))"
    r"|\b(?:planned|forthcoming)\s+publication\b"
    r"|\b(?:cite|cites|cited|citing)\s+(?:the\s+)?published\s+"
    r"(?:articles?|papers?|works?|versions?|studies?)\b"
    r"|\b(?:previously|recently|already)\s+published\s+"
    r"(?:articles?|papers?|works?|versions?|studies?|results?)\b"
    r"|\bpublished\s+(?:articles?|papers?|works?|versions?|studies?|"
    r"literature|datasets?|benchmarks?)\b"
    r"|\bpublish(?:ed|es|ing)?\s+(?:the\s+)?"
    r"(?:code|data|datasets?|results?|artifacts?|repository)\b"
    r"|\b(?-i:ACCEPT)\b"
    r"|\bkinematic\s+rejection\s+tests?\b"
    r"|\baverage\s+acceptance\s+tokens?\b"
    r"|\bacceptance[-\s](?:throughput|rate)\b"
    r"|\b(?:cost[-\s]derived\s+)?acceptance\s+gate\b"
    r"|\b(?:low|high)\s+acceptance\b"
    r"|\bacceptance\s+rates?\b"
    r"|\bdriver\s+order\s+acceptance\s+behavio(?:u)?r\b"
    r"|\b(?:hallucination|side[-\s]talk)\s+rejection\b"
    r"|\b(?:a\s+)?state['’]?s\s+acceptance\b"
    r"|\brejection\s+(?:criteria|threshold)\b"
    r"|\brejection[-\s]based\s+fine[-\s]tuning\b"
    r"|\bclassification[-\s]with[-\s]rejection\b"
    r"|\bnon[-\s]accepting\s+states?\b"
    r"|\baccept\s+states?\b"
    r"|\b(?:agent|model)\s+explicitly\s+rejects?\s+goal\s+updates?\b"
    r"|\binterpretable\s+reject\s+option\b"
    r"|\b(?:instructions?|outputs?|responses?)\s+incorrectly\s+rejected\s+"
    r"by\s+(?:safety\s+)?models?\b"
    r"|\bheuristics?\s+for\s+rejecting\s+priors?\b"
    r"|\bdesk[-\s]rejection\s+(?:vectors?|pools?|polic(?:y|ies)|algorithms?|"
    r"process(?:es)?|mechanisms?|optimizations?|problems?|constraints?|rates?|"
    r"data|datasets?|simulations?)\b"
    r"|\bpost[-\s]rejection\s+(?:pools?|sets?)\b"
    r"|\bdesk[-\s]accepted\s+for\s+review\b"
    r"|\b(?:select(?:s|ed|ing)?|decid(?:e|es|ed|ing))\s+which\s+papers\s+"
    r"to\s+reject\b"
    r"|\b(?:rejecting|accepting)\s+[^.!?\n]{0,40}\b"
    r"(?:tokens?|samples?|proposals?|outputs?)\b"
    r"|\brejection\s+constants?\b"
    r"|\bacceptance\s+rules?\b"
    r"|\bacceptance\s+based\s+on\s+target[-\s]proposal\b"
    r"|\bfirst\s+rejected\s+sample\b"
    r"|\bsamples?\b[^.!?\n]{0,50}\b(?:rejected\s+or\s+accepted|"
    r"accepted\s+or\s+rejected)\b"
    r"|\brejection[-\s]related\s+(?:words?|tokens?|keywords?)\b"
    r"|\breject(?:s|ed|ing)?\s+(?:normal|harmless|safe)\s+"
    r"(?:queries|requests|inputs)\b"
    r"|\b(?:reject(?:ing)?|rejection)/?[-\s]?answer(?:ing)?\s+"
    r"behavio(?:u)?rs?\b"
    r"|\brejection\s+(?:responses?|behavio(?:u)?rs?)\b"
    r"|\brejection\s+(?:policy|rates?|keywords?|subspace)\b"
    r"|\b(?:high|low|early)\s+rejection\b"
    r"|\bacceptance\s+or\s+refusal\b"
    r"|\bpreferred\s+minus\s+rejected\b"
    r"|\b(?:determinants?\s+of\s+|influences?\s+|affects?\s+|"
    r"predicts?\s+|correlates?\s+with\s+)?paper\s+"
    r"(?:acceptance|rejection)\b"
    r"|\bpaper\s+(?:acceptance|rejection)\s+(?:or\s+(?:acceptance|rejection)\s+)?"
    r"(?:decisions?|outcomes?|statuses?|patterns?|chances?|rates?|variables?)\b"
    r"|\b(?:paper|submission|conference)\s+(?:acceptance|rejection)\s+"
    r"(?:decisions?|outcomes?|statuses?|patterns?|chances?|rates?|variables?|labels?)\b"
    r"|\b(?:acceptance|rejection)\s+(?:decisions?|outcomes?|statuses?|patterns?|"
    r"chances?|rates?|variables?|labels?)\b[^.!?\n]{0,50}\b"
    r"(?:dataset|corpus|study|analysis|prediction|predictor|model(?:l)?ing|"
    r"classification|regression)\b"
    r"|\b(?:chance|chances|likelihood|odds|probability|probabilities)\s+of\s+"
    r"acceptance\b"
    r"|\b(?:accepted|rejected)\s+papers\b"
    r"|\bstyle/acceptance\s+patterns\b"
    r"|\bacceptance\s+as\s+(?:an?\s+)?(?:outcome|dependent\s+variable|label)\b"
    r"|\b(?:published\s+)(?:[A-Za-z0-9-]+\s+){0,5}"
    r"(?:articles?|papers?|works?|versions?|studies?|models?|benchmarks?)\b"
    r")"
)
_HISTORICAL_TECHNICAL_CONFIDENCE_CONTEXT_RE = re.compile(
    r"(?i)(?:"
    r"\bconfidence\s+scores?\s+of\s+(?:the\s+)?object\s+detector\b|"
    r"\b(?:user|reader)\b[^.!?\n]{0,100}\b(?:LLM|model)['’]?s?\s+answer\b|"
    r"\b(?:forward|backward)\s+confidence\s+scores?\b|"
    r"\bconfidence\s+score\s+(?:calculation|usage|prediction|calibration)\b|"
    r"\bimprove\s+confidence\s+in\s+(?:the|this|their)\s+"
    r"(?:method|approach|model|result|analysis|framework)\b"
    r")"
)
_HISTORICAL_TECHNICAL_CONFIDENCE_TERM_RE = re.compile(
    r"(?i)(?:"
    r"\bconfidence\s+scores?\b|"
    r"\bconfidence\s+in\s+(?:the|this|their)\s+"
    r"(?:method|approach|model|result|analysis|framework)\b"
    r")"
)
_HISTORICAL_CONTEXTUAL_DECISION_TERM_RE = re.compile(
    r"(?i)(?:"
    r"\b(?:accept|reject)(?:ance|ion|ed|ing)?\b|"
    r"\b(?:publishable|unpublishable|resubmit|resubmission)\b|"
    r"\bpublication\b|"
    r"\b(?:venue|conference)\s+(?:bar|standard|criteria|fit)\b|"
    r"\b(?:bar|standard|criteria|fit|suitable|appropriate|ready|limited|"
    r"insufficient|weak|strong|specific|incremental|novel|substantial|"
    r"significant|enough|shape)\b[^.!?\n]{0,50}\b(?:venue|conference)\b"
    r")"
)
_HISTORICAL_DECISION_KEYWORD_RE = re.compile(
    r"(?i)\b(?:"
    r"accept(?:ance|ed|ing)?|"
    r"reject(?:ion|ed|ing)?|"
    r"publish(?:able|ed|ing)?|"
    r"publication"
    r")\b"
)
_HISTORICAL_SCORE_FRAGMENT_RE = re.compile(
    r"(?i)(?:"
    r"\b(?:i|we)\s+(?:therefore\s+)?(?:believe|think)\s+(?:that\s+)?"
    r"(?:it|this)\s+(?:falls?|lands?)\s+(?:on|in)\s+(?:the\s+)?borderline\b|"
    r"\b(?:i|we)\b[^.!?\n]{0,40}\bcannot\s+give\s+(?:an?\s+)?"
    r"(?:(?:strong|weak|positive|negative|borderline|marginal)\s+)?"
    r"(?:acceptance|rejection|accept|reject)\s+recommendation\b"
    r"(?:\s+(?:till|for)\s+now)?|"
    r"\b(?:i|we)\b[^.!?\n]{0,50}\bconsider(?:ed|ing)?\s+(?:an?\s+)?"
    r"(?:higher|lower)\s+(?:score|rating)\b|"
    r"\b(?:i|we)\b[^.!?\n]{0,60}\bconsider(?:ed|ing)?\s+raising\s+"
    r"to\s+(?:an?\s+)?score\s+of\s+\d+(?:\.\d+)?"
    r"(?:\s+or\s+\d+(?:\.\d+)?)?|"
    r"\b(?:i|we|this\s+reviewer|the\s+reviewer|reviewer)\b"
    r"[^.!?\n]{0,80}\b(?:consider(?:ed|ing)?\s+)?"
    r"(?:raise|raising|increase|increasing|modify|modifying|change|changing|"
    r"adjust|adjusting|revisit|revisiting|improve|improving|update|updating|"
    r"upgrade|upgrading|revise|revising|reconsider|reconsidering)\b"
    r"[^.!?\n]{0,25}\b(?:my|our|the|this|their)\s+"
    r"(?:(?:overall|evaluation|final|partial|soundness|presentation|"
    r"contribution)\s+)*(?:review\s+)?(?:scores?|ratings?)\b"
    r"(?:\s+(?:to|by)\s+(?:an?\s+)?\d+(?:\.\d+)?"
    r"(?:\s*(?:/|[-–—])\s*\d+(?:\.\d+)?|"
    r"\s+or\s+(?:\d+(?:\.\d+)?|higher|lower))?|"
    r"\s+from\s+(?:an?\s+)?\d+(?:\.\d+)?\s+to\s+(?:an?\s+)?"
    r"\d+(?:\.\d+)?)?|"
    r"\b(?:this|the)\s+reviewer\b[^.!?\n]{0,80}\b"
    r"willingness\s+to\s+(?:raise|increase|modify|change|adjust)\b"
    r"[^.!?\n]{0,30}\b(?:their\s+)?(?:scores?|ratings?)\b|"
    r"\b(?:the\s+)?(?:scores?|ratings?)\s+(?:will|would|can|could)\s+be\s+"
    r"(?:raised|increased|modified|changed|adjusted|revisited)\b|"
    r"\b(?:revisit|reconsider)\s+(?:this|the|my|our)\s+(?:score|rating)\b|"
    r"\b(?:justification|rationale|reason)\s+for\s+"
    r"(?:(?:my|our|the|this)\s+)?(?:(?:overall|final|soundness|presentation|"
    r"contribution|cautious)\s+)*(?:scores?|ratings?)\b|"
    r"\b(?:to\s+achieve|leads?\s+me\s+to)\s+(?:an?\s+)?score\s+of\s+"
    r"\d+(?:\.\d+)?(?:\s*[-–]\s*\d+(?:\.\d+)?)?|"
    r"\b(?:i|we)\b[^.!?\n]{0,40}\bscore\s+(?:this|the)\s+paper\s+"
    r"(?:very\s+)?(?:highly|poorly)\b|"
    r"\b(?:i|we)\b[^.!?\n]{0,40}\bopen\s+to\s+(?:an?\s+)?"
    r"(?:high|low|higher|lower)\s+(?:score|rating)\b|"
    r"\b(?:cannot\s+give|give|gave|offer|offered|make|made)\s+(?:an?\s+)?"
    r"(?:(?:strong|weak|positive|negative|borderline|marginal)\s+)?"
    r"(?:acceptance|rejection|accept|reject)\s+recommendation\b|"
    r"\b(?:rating|score)\s+of\s+(?:borderline|boardline|strong|weak)\s+"
    r"(?:accept|reject)\b|"
    r"\b(?:i['’]?m|i\s+am|we\s+are)\s+rating\s+(?:the\s+)?"
    r"(?:presentation|soundness|contribution)\s+(?:component\s+)?as\s+"
    r"(?:only\s+)?\d+(?:\.\d+)?\b|"
    r"\b(?:the\s+)?difference\s+between\s+(?:an?\s+)?score\s+of\s+"
    r"\d+(?:\.\d+)?\s+and\s+\d+(?:\.\d+)?\b|"
    r"\b(?:higher|lower)\s+score\s+of\s+\d+(?:\.\d+)?\b"
    r"|,?\s*\b(?:hence|thus|therefore)\s+my\s+"
    r"(?:(?:initial|current|final|overall)\s+)?"
    r"(?:(?:low|high|lower|higher)\s+)?(?:score|rating)\b"
    r"|^\s*(?:am|are)\s+open\s+to\s+(?:potentially\s+)?"
    r"(?:rais(?:e|ing)|increas(?:e|ing)|improv(?:e|ing)|updat(?:e|ing)|"
    r"revis(?:e|ing)|reconsider(?:ing)?)\s+"
    r"(?:my|our|the)\s+(?:score|rating)\b"
    r"|\bmy\s+(?:(?:main|primary)\s+)?ask\s+to\s+"
    r"(?:improve|raise|increase|change|update|revise)\s+(?:the|my)\s+"
    r"rating\s+of\s+(?:this|the)\s+paper\s+is\s+(?:to\s+)?"
    r"|\b(?:i|we)\s+(?:am\s+)?(?:rating|scoring)\s+(?:this|the)\s+paper\s+"
    r"\d+(?:\.\d+)?\s*/\s*\d+(?:\.\d+)?(?:\s+instead\s+of\s+"
    r"\d+(?:\.\d+)?\s*/\s*\d+(?:\.\d+)?)?\b"
    r"|\b(?:i|we)\b[^.!?\n]{0,50}\brecommend\s+(?:an?\s+)?score\s+of\s+"
    r"\d+(?:\.\d+)?(?:\s*/\s*\d+(?:\.\d+)?)?(?:\s*\([^)]*\))?"
    r"|\b(?:i|we)\b[^.!?\n]{0,50}\bprovide\s+(?:an?\s+)?score\s+of\s+"
    r"\d+(?:\.\d+)?(?:\s*/\s*\d+(?:\.\d+)?)?"
    r"|\b(?:i|we)\b[^.!?\n]{0,50}\braise\s+it\s+to\s+"
    r"\d+(?:\.\d+)?(?:\s*(?:/|[-–—])\s*\d+(?:\.\d+)?|"
    r"\s+or\s+(?:\d+(?:\.\d+)?|higher|lower))?(?:\s*\([^)]*\))?"
    r"|\b(?:leaning|inclined)\s+toward\s+(?:the\s+)?rating\s+[\"'“”]?"
    r"\d+(?:\.\d+)?(?:\s*\([^)]*\))?[\"'“”]?"
    r"|\b(?:make|makes|made|making)?\s*(?:me|us)?\s*(?:to\s+)?"
    r"recommend(?:ing)?\s+(?:this|the)?\s*(?:paper|work|submission)?\s*"
    r"(?:an?\s+)?(?:score|rating)\s+of\s+\d+(?:\.\d+)?"
    r"(?:\s*[-–]\s*\d+(?:\.\d+)?)?(?:\s*/\s*\d+(?:\.\d+)?)?"
    r"(?:\s*\([^)]*\))?"
    r"|\b(?:a\s+)?(?:score|rating)\s+of\s+"
    r"(?:one|two|three|four|five|six|seven|eight|nine|ten|\d+(?:\.\d+)?)"
    r"(?:\s+or\s+(?:higher|lower))?(?:\s*\([^)]*\))?"
    r"(?=[^.!?\n]{0,30}\b(?:accept|reject|rating|score|review|rebuttal)\b)"
    r"|\b(?:change|changing|changed)\s+(?:my|our|the)\s+review\s+from\s+"
    r"[\"'“”]?(?:weak|strong|borderline)?\s*(?:accept|reject)[\"'“”]?\s+to\s+"
    r"[\"'“”]?(?:weak|strong|borderline)?\s*(?:accept|reject)[\"'“”]?"
    r"|\b(?:reason|rationale)\s+behind\s+(?:my|our|the)\s+"
    r"(?:weak|strong|borderline)?\s*(?:accept|reject)\s+rating\b"
    r")"
)
_HISTORICAL_NUMERIC_REVIEW_RE = re.compile(
    r"(?i)(?:"
    r"^\s*(?:overall\s+)?(?:score|rating)\s*[:=]\s*"
    r"(?:\d+(?:\.\d+)?(?:\s*/\s*\d+)?|strong|weak|borderline)|"
    r"\b(?:overall|initial|final|current|reviewer)\s+rating\b|"
    r"\breviewer\s+score\b|"
    r"\b(?:rating)\s*(?:of|is|was|:|=)\s*\d+(?:\.\d+)?"
    r"(?:\s*/\s*\d+)?\b|"
    r"(?<!\()\bi\b(?!\))[^.!?\n]{0,55}\b"
    r"(?:give|gave|assign|assigned|rate|rated|put)\b\s+"
    r"(?:(?:(?:this|the)\s+(?:paper|submission|work)|it)\s+)?"
    r"(?:an?\s+)?(?:(?:score|rating)\s+(?:of\s+)?)?"
    r"(?<![-\w.])\d+(?:\.\d+)?"
    r"(?:\s*/\s*\d+(?:\.\d+)?|\s+(?:points?|score|rating))"
    r"(?![-\w.])|"
    r"\breviewer\b[^.!?\n]{0,80}\b"
    r"(?:gives?|gave|assigns?|assigned|rates?|rated)\b[^.!?\n]{0,40}"
    r"\b\d+(?:\.\d+)?(?:\s*/\s*\d+)?\b|"
    r"\b(?:justify|justifies|justified|justifying)\b[^.!?\n]{0,50}"
    r"\brating\s+(?:of\s+)?\d+(?:\.\d+)?\b"
    r")"
)
_HISTORICAL_AUTHOR_GUIDANCE_RE = re.compile(
    r"(?i)\b(?:my|our)\s+recommendation\s+(?:(?:to|for)\s+(?:the\s+)?"
    r"authors?\s+(?:(?:is|would\s+be)\s+)?to|(?:is|would\s+be)\s+that\s+"
    r"(?:the\s+)?authors?)\s+"
    r"(?=(?:add|include|provide|clarify|explain|compare|discuss|evaluate|"
    r"report|revise|remove|expand|justify|document|address|narrow|improve|"
    r"focus)\b)"
)

Embedder = Callable[[list[str]], Awaitable[list[list[float]]]]


def _is_owned_index(path: Path) -> bool:
    """Return whether ``path`` is an owned review-memory directory."""

    if not path.is_dir() or path.is_symlink():
        return False
    header = path / _INDEX_HEADER
    if not header.is_file() or header.is_symlink():
        return False
    try:
        raw = header.read_bytes()
        if len(raw) > 1024 * 1024:
            return False
        value = json.loads(raw)
        return isinstance(value, dict) and value.get("index_owner") == INDEX_OWNER
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False


class _BuildLock:
    """A small cross-process lock preventing interleaved index builders."""

    def __init__(self, destination: Path) -> None:
        self._path = Path(f"{destination}.build.lock")
        self._handle: Any | None = None

    def __enter__(self) -> Self:
        if self._path.is_symlink():
            raise ValueError(f"refusing symlink build lock: {self._path}")
        self._handle = self._path.open("a+b")
        try:
            try:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except ImportError:  # pragma: no cover - Windows fallback
                import msvcrt

                msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
        except (BlockingIOError, OSError):
            self._handle.close()
            self._handle = None
            raise RuntimeError(
                f"another process is already building review index {self._path}"
            ) from None
        return self

    def __exit__(self, *_args: object) -> None:
        if self._handle is None:
            return
        try:
            try:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            except ImportError:  # pragma: no cover - Windows fallback
                import msvcrt

                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            self._handle.close()
            self._handle = None


def _validate_owned_destination(destination: Path) -> None:
    if destination.is_symlink():
        raise ValueError(f"refusing symlink index path: {destination}")
    if not destination.exists():
        return
    if not _is_owned_index(destination):
        raise ValueError(
            "refusing to modify an existing path that is not an Omni paper-review "
            f"memory index directory: {destination}"
        )
    try:
        _read_index_snapshot(destination, validate_artifacts=True)
    except (OSError, TypeError, ValueError) as exc:
        raise ValueError(
            "refusing to modify an invalid or corrupted Omni paper-review "
            f"memory index directory: {destination}"
        ) from exc


def _within_root(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _clean_text(value: Any) -> str:
    text = str(value or "")
    return "".join(
        character
        for character in text
        if character in "\n\r\t" or ord(character) >= 32
    ).strip()


_DECISION_REDACTION_MARKER = "[Historical score or decision statement removed.]"
_VENUE_REDACTION_MARKER = "[Historical venue-fit conclusion removed.]"
_IDENTITY_REDACTION_MARKER = "[Historical reviewer identity removed.]"
_PRIOR_SUBMISSION_REDACTION_MARKER = (
    "[Historical prior-submission detail removed.]"
)
_HISTORICAL_SCORE_STANCE_CUE_RE = re.compile(
    r"(?i)(?:"
    r"\b(?:score|rating|assessment|recommendation)\b[^.!?\n]{0,70}\b"
    r"(?:accept|reject|positive|negative|raise|increase|improve|reconsider|consider)\b|"
    r"\b(?:accept|reject|raise|increase|improve|reconsider|consider|give)\b"
    r"[^.!?\n]{0,70}\b(?:score|rating|assessment|recommendation)\b|"
    r"\b(?:borderline|weak|strong)\s+(?:accept|reject)\b"
    r")"
)
_HISTORICAL_SCORE_PURPOSE_ACTION_RE = re.compile(
    r"(?is)^\s*to\s+(?:improve|raise|increase|change)\b"
    r"[^,;]{0,100}\b(?:score|rating)\b[^,;]*[,;]\s*"
    r"(?P<action>(?:i|we)\s+(?:would\s+like|want|need|ask)\b.+)$"
)
_HISTORICAL_CONDITION_CONNECTOR_RE = re.compile(
    r"(?i)\b(?:if|provided(?:\s+that)?|unless)\b"
)


def _looks_like_historical_score_stance(value: str) -> bool:
    """Return whether a clause is reviewer stance rather than author evidence."""

    probe = re.sub(r"[*_`~]+", "", value)
    return bool(
        _HISTORICAL_SCORE_STANCE_CUE_RE.search(probe)
        or _HISTORICAL_EXPLICIT_REVIEW_METADATA_RE.search(probe)
        or _HISTORICAL_FIRST_PERSON_SCORE_RE.search(probe)
        or _HISTORICAL_REVIEW_VERDICT_RE.search(probe)
        or _HISTORICAL_NUMERIC_REVIEW_RE.search(probe)
    )


def _redact_score_stance_keep_action(piece: str) -> str:
    """Split reviewer stance from a concrete condition or requested revision."""

    purpose = _HISTORICAL_SCORE_PURPOSE_ACTION_RE.match(piece)
    if purpose is not None:
        return f"{_DECISION_REDACTION_MARKER}\n{purpose.group('action').strip()}"

    connectors = list(_HISTORICAL_CONDITION_CONNECTOR_RE.finditer(piece))
    for connector in reversed(connectors):
        stance = piece[: connector.start()].rstrip(" ,;:")
        condition = piece[connector.start() :].strip()
        if stance and condition and _looks_like_historical_score_stance(stance):
            return f"{_DECISION_REDACTION_MARKER}\n{condition}"

    if re.match(r"(?i)^\s*if\b", piece):
        for comma in reversed([match.start() for match in re.finditer(",", piece)]):
            stance = piece[comma + 1 :].strip()
            if stance and _looks_like_historical_score_stance(stance):
                condition = piece[:comma].rstrip()
                return f"{condition}\n{_DECISION_REDACTION_MARKER}"
    return piece


def _redact_decision_fragments(piece: str) -> str:
    """Redact verdict fragments while preserving protected technical language."""

    piece = _redact_score_stance_keep_action(piece)
    protected: list[str] = []

    def protect(match: re.Match[str]) -> str:
        token = f"\ue000{len(protected)}\ue001"
        protected.append(match.group(0))
        return token

    working = _HISTORICAL_TECHNICAL_DECISION_TERM_RE.sub(protect, piece)
    if _HISTORICAL_TECHNICAL_CONFIDENCE_CONTEXT_RE.search(piece):
        working = _HISTORICAL_TECHNICAL_CONFIDENCE_TERM_RE.sub(protect, working)
    working = _HISTORICAL_IDENTITY_FRAGMENT_RE.sub(
        _IDENTITY_REDACTION_MARKER,
        working,
    )
    working = _HISTORICAL_PRIOR_CONTEXT_FRAGMENT_RE.sub(
        _PRIOR_SUBMISSION_REDACTION_MARKER,
        working,
    )
    working = _HISTORICAL_SCORE_FRAGMENT_RE.sub(
        _DECISION_REDACTION_MARKER,
        working,
    )
    working = _HISTORICAL_DECISION_FRAGMENT_RE.sub(
        _DECISION_REDACTION_MARKER,
        working,
    )
    working = _HISTORICAL_PRIOR_SUBMISSION_DETAIL_RE.sub(
        _PRIOR_SUBMISSION_REDACTION_MARKER,
        working,
    )
    # Venue expressions often contain publication vocabulary. Match the full
    # venue-fit clause first so the more general verdict pass cannot split it.
    working = _HISTORICAL_VENUE_FIT_FRAGMENT_RE.sub(
        _VENUE_REDACTION_MARKER,
        working,
    )
    working = _HISTORICAL_ALTERNATE_VENUE_RE.sub(
        _VENUE_REDACTION_MARKER,
        working,
    )
    # Any unprotected acceptance/publication vocabulary is a historical verdict
    # cue. Redacting the cue instead of its whole sentence retains the reason or
    # author action around it. Technical uses were replaced by opaque tokens above.
    working = _HISTORICAL_DECISION_KEYWORD_RE.sub(
        _DECISION_REDACTION_MARKER,
        working,
    )
    for index, original in enumerate(protected):
        working = working.replace(f"\ue000{index}\ue001", original)
    return working


def _redact_historical_review_text(value: Any) -> str:
    """Remove explicit reviewer identity and score/decision language.

    Historical reviews are used to discover concerns, not to transfer reviewer
    identity or acceptance priors. Redaction is deterministic and sentence/line
    bounded; it does not summarize or otherwise rewrite the retained critique.
    """

    text = _clean_text(value)
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # OpenReview prose sometimes contains a visual line wrap in the middle of
    # a decision phrase (for example, ``recommend\nrejection``). Join only that
    # narrow construction so ordinary Markdown list boundaries stay intact.
    text = _HISTORICAL_WRAPPED_DECISION_RE.sub(r"\1 ", text)
    pieces = re.split(
        r"(\n+|(?<=[.!?])\s+|(?<=[.!?][\"'”’])\s+|"
        r"(?<=[.!?]\*\*)\s+|(?<=[.!?][)\]])\s+)",
        text,
    )
    redacted: list[str] = []
    for piece in pieces:
        if not piece or piece.isspace():
            redacted.append(piece)
            continue
        decision_redacted_piece = _redact_decision_fragments(piece)
        decision_probe = decision_redacted_piece.replace(
            _DECISION_REDACTION_MARKER,
            "",
        )
        decision_probe = decision_probe.replace(_VENUE_REDACTION_MARKER, "")
        decision_probe = decision_probe.replace(_IDENTITY_REDACTION_MARKER, "")
        decision_probe = decision_probe.replace(
            _PRIOR_SUBMISSION_REDACTION_MARKER,
            "",
        )
        decision_probe = _HISTORICAL_TECHNICAL_DECISION_TERM_RE.sub(
            "",
            decision_probe,
        )
        # Markdown emphasis can split a semantic label (for example,
        # ``**reject** rating``). Normalize markup only in the detection copy;
        # retained review prose is never rewritten.
        decision_probe = re.sub(r"[*_`~]+", "", decision_probe)
        if _HISTORICAL_TECHNICAL_CONFIDENCE_CONTEXT_RE.search(decision_probe):
            decision_probe = _HISTORICAL_TECHNICAL_CONFIDENCE_TERM_RE.sub(
                "",
                decision_probe,
            )
        decision_probe = _HISTORICAL_AUTHOR_GUIDANCE_RE.sub("", decision_probe)
        if (
            _HISTORICAL_IDENTITY_RE.search(decision_probe)
            or _HISTORICAL_PRIOR_REVIEW_RE.search(decision_probe)
        ):
            redacted.append(_IDENTITY_REDACTION_MARKER)
        elif (
            _HISTORICAL_SCORE_OR_DECISION_RE.search(decision_probe)
            or _HISTORICAL_EXPLICIT_REVIEW_METADATA_RE.search(decision_probe)
            or _HISTORICAL_FIRST_PERSON_SCORE_RE.search(decision_probe)
            or _HISTORICAL_REVIEW_CONFIDENCE_RE.search(decision_probe)
            or _HISTORICAL_IMPLICIT_DECISION_RE.search(decision_probe)
            or _HISTORICAL_REVIEW_VERDICT_RE.search(decision_probe)
            or _HISTORICAL_NUMERIC_REVIEW_RE.search(decision_probe)
        ):
            redacted.append(_DECISION_REDACTION_MARKER)
        else:
            redacted.append(decision_redacted_piece)
    joined = "".join(redacted)
    joined = re.sub(
        r"\(\[Historical prior-submission detail removed\.\]\s+([^()\n]+)\)",
        r"[Historical prior-submission detail removed.] \1",
        joined,
    )
    for marker in (
        _DECISION_REDACTION_MARKER,
        _VENUE_REDACTION_MARKER,
        _IDENTITY_REDACTION_MARKER,
        _PRIOR_SUBMISSION_REDACTION_MARKER,
    ):
        joined = re.sub(rf"(?<=[A-Za-z0-9]){re.escape(marker)}", f" {marker}", joined)
        joined = re.sub(rf"{re.escape(marker)}(?=[A-Za-z0-9])", f"{marker} ", joined)
    joined = re.sub(
        rf"{re.escape(_DECISION_REDACTION_MARKER)}\s*,?\s+(?i:if)\s+",
        f"{_DECISION_REDACTION_MARKER}\nIf ",
        joined,
    )
    joined = re.sub(
        rf"{re.escape(_VENUE_REDACTION_MARKER)}\s*,?\s+(?i:unless)\s+",
        f"{_VENUE_REDACTION_MARKER}\nUnless ",
        joined,
    )
    return joined.strip()


def _flat_text(value: Any) -> str:
    return _SPACE_RE.sub(" ", _clean_text(value)).strip()


def review_index_setup_command(
    manifest_path: str | Path | None = None,
    index_path: str | Path | None = None,
    *,
    rebuild: bool = False,
) -> str:
    """Return a build command that works outside the repository root."""

    script = shlex.quote(str(_SKILL_DIR / "scripts" / "build_review_index.py"))
    manifest = shlex.quote(str(manifest_path or "<MANIFEST_BODY_JSONL>"))
    index = shlex.quote(str(index_path or "<REVIEW_INDEX_DIRECTORY>"))
    command = f"python3 {script} --manifest {manifest} --index {index}"
    return f"{command} --rebuild" if rebuild else command


def _normalized_title(value: Any) -> str:
    return _flat_text(value).casefold()


def _content_value(content: Any, field: str) -> Any:
    if not isinstance(content, dict):
        return ""
    value = content.get(field)
    if isinstance(value, dict) and "value" in value:
        return value.get("value")
    return value


def paper_embedding_text(title: Any, abstract: Any) -> str:
    """Build the stable paper-level representation used on both index sides."""

    clean_title = _flat_text(title)
    clean_abstract = _flat_text(abstract)
    return f"{clean_title} [SEP] {clean_abstract}".strip()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _resolve_source_path(
    value: Any,
    manifest_path: Path,
    *,
    allowed_data_root: Path,
) -> Path:
    candidate = Path(str(value or "")).expanduser()
    if not candidate.is_absolute():
        candidate = manifest_path.parent / candidate
    resolved = candidate.resolve()
    if not _within_root(resolved, allowed_data_root):
        raise ValueError(
            f"manifest source path is outside the allowed data root: {resolved}"
        )
    return resolved


def _review_content(review: dict[str, Any]) -> dict[str, Any]:
    raw_content = review.get("content")
    if not isinstance(raw_content, dict):
        return {}
    out: dict[str, Any] = {}
    for field in raw_content:
        name = str(field)
        if name.casefold() not in _AUTHOR_GUIDANCE_FIELDS:
            continue
        value = _content_value(raw_content, name)
        if isinstance(value, str):
            cleaned: Any = _redact_historical_review_text(value)
        elif isinstance(value, list):
            cleaned = [
                redacted
                for item in value
                if (redacted := _redact_historical_review_text(item))
            ]
        else:
            cleaned = value
        if cleaned is not None and cleaned != "" and cleaned != []:
            out[name] = cleaned
    return out


def _manifest_record(
    row: dict[str, Any],
    *,
    manifest_path: Path,
    source_line: int,
    allowed_data_root: Path,
) -> dict[str, Any]:
    paper_id = _flat_text(
        row.get("source_paper_id")
        or row.get("openreview_forum_id")
        or row.get("paper_id")
    )
    if not paper_id:
        raise ValueError(f"manifest line {source_line} has no paper id")
    review_path = _resolve_source_path(
        row.get("reviews_json_path"),
        manifest_path,
        allowed_data_root=allowed_data_root,
    )
    if not review_path.is_file():
        raise FileNotFoundError(
            f"manifest line {source_line} review JSON does not exist: {review_path}"
        )
    review_size = review_path.stat().st_size
    if review_size > MAX_REVIEW_JSON_BYTES:
        raise ValueError(
            f"review JSON for {paper_id} exceeds {MAX_REVIEW_JSON_BYTES} bytes"
        )
    review_bytes = review_path.read_bytes()
    actual_review_hash = _sha256_bytes(review_bytes)
    expected_json_hash = _flat_text(row.get("reviews_json_sha256"))
    if expected_json_hash and expected_json_hash != actual_review_hash:
        raise ValueError(
            f"review JSON hash mismatch for {paper_id}: expected "
            f"{expected_json_hash}, found {actual_review_hash}"
        )
    expected_aggregate_hash = _flat_text(row.get("reviews_sha256"))
    review_paths = row.get("review_paths")
    aggregate_review_path: Path | None = None
    if isinstance(review_paths, list) and review_paths:
        aggregate_review_path = _resolve_source_path(
            review_paths[0],
            manifest_path,
            allowed_data_root=allowed_data_root,
        )
    if expected_aggregate_hash:
        if aggregate_review_path is None or not aggregate_review_path.is_file():
            raise ValueError(
                f"manifest has reviews_sha256 for {paper_id} but no readable "
                "aggregate review_path"
            )
        aggregate_hash = _sha256_file(aggregate_review_path)
        if expected_aggregate_hash != aggregate_hash:
            raise ValueError(
                f"aggregate review hash mismatch for {paper_id}: expected "
                f"{expected_aggregate_hash}, found {aggregate_hash}"
            )
    try:
        review_data = json.loads(review_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid review JSON for {paper_id}: {review_path}") from exc
    if not isinstance(review_data, dict):
        raise TypeError(f"review JSON for {paper_id} is not an object")

    submission = review_data.get("submission")
    submission = submission if isinstance(submission, dict) else {}
    source_identifiers = {
        _flat_text(value)
        for value in (
            row.get("source_paper_id"),
            row.get("openreview_forum_id"),
            review_data.get("paper_id"),
            submission.get("id"),
            submission.get("forum"),
        )
        if _flat_text(value)
    }
    if source_identifiers != {paper_id}:
        raise ValueError(
            f"paper identity mismatch for manifest line {source_line}: "
            + ", ".join(sorted(source_identifiers))
        )
    submission_content = (
        submission.get("content")
        if isinstance(submission.get("content"), dict)
        else {}
    )
    abstract = _clean_text(_content_value(submission_content, "abstract"))
    title = _clean_text(
        row.get("title")
        or review_data.get("title")
        or _content_value(submission_content, "title")
    )
    if not title or not abstract:
        raise ValueError(f"paper {paper_id} requires a non-empty title and abstract")

    reviews = review_data.get("official_reviews")
    if not isinstance(reviews, list) or not reviews:
        raise ValueError(f"paper {paper_id} has no official reviews")
    stored_reviews: list[dict[str, Any]] = []
    for ordinal, raw_review in enumerate(reviews, 1):
        if not isinstance(raw_review, dict):
            raise TypeError(f"paper {paper_id} review {ordinal} is not an object")
        content = _review_content(raw_review)
        if not any(_clean_text(value) for value in content.values()):
            raise ValueError(f"paper {paper_id} review {ordinal} has no content")
        stored_reviews.append({"content": content})

    retrieval_text = paper_embedding_text(title, abstract)
    reviews_raw = json.dumps(
        stored_reviews,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(reviews_raw) > MAX_REVIEW_PACKET_BYTES:
        raise ValueError(
            f"stored review packet for {paper_id} exceeds "
            f"{MAX_REVIEW_PACKET_BYTES} bytes"
        )
    reviews_blob = zlib.compress(
        reviews_raw,
        level=6,
    )
    paper_path = _resolve_source_path(
        row.get("paper_path"),
        manifest_path,
        allowed_data_root=allowed_data_root,
    )
    if not paper_path.is_file():
        raise FileNotFoundError(
            f"manifest line {source_line} paper body does not exist: {paper_path}"
        )
    actual_paper_hash = _sha256_file(paper_path)
    expected_paper_hash = _flat_text(row.get("paper_sha256"))
    if expected_paper_hash and expected_paper_hash != actual_paper_hash:
        raise ValueError(
            f"paper body hash mismatch for {paper_id}: expected "
            f"{expected_paper_hash}, found {actual_paper_hash}"
        )
    return {
        "paper_id": paper_id,
        "title": title,
        "normalized_title": _normalized_title(title),
        "abstract": abstract,
        "retrieval_text": retrieval_text,
        "retrieval_hash": _sha256_bytes(retrieval_text.encode("utf-8")),
        "paper_sha256": actual_paper_hash,
        "reviews_json_sha256": actual_review_hash,
        "source_line": source_line,
        "review_count": len(stored_reviews),
        "reviews_blob": reviews_blob,
        "reviews_raw_bytes": len(reviews_raw),
        "reviews_raw_sha256": _sha256_bytes(reviews_raw),
        "conference": _flat_text(row.get("conference") or review_data.get("conference")),
    }


def _iter_jsonl(path: Path) -> Iterable[tuple[int, dict[str, Any]]]:
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSON on manifest line {line_number}") from exc
            if not isinstance(row, dict):
                raise TypeError(f"manifest line {line_number} is not an object")
            yield line_number, row


def _manifest_has_record(path: Path) -> bool:
    """Return whether a JSONL manifest contains at least one nonblank record."""

    with path.open("r", encoding="utf-8") as handle:
        return any(line.strip() for line in handle)


def _normalize_vector(vector: list[float]) -> list[float]:
    values = [float(item) for item in vector]
    if not values or any(not math.isfinite(item) for item in values):
        raise ValueError("embedding provider returned an empty or non-finite vector")
    scale = max(abs(item) for item in values)
    if scale <= 0.0:
        raise ValueError("embedding provider returned a zero vector")
    scaled = [item / scale for item in values]
    scaled_norm = math.sqrt(sum(item * item for item in scaled))
    normalized = [item / scaled_norm for item in scaled]
    normalized_norm = math.sqrt(sum(item * item for item in normalized))
    if (
        any(not math.isfinite(item) for item in normalized)
        or not math.isclose(normalized_norm, 1.0, rel_tol=1e-12, abs_tol=1e-12)
    ):
        raise ValueError("embedding provider returned a vector that cannot be normalized")
    return normalized


def _pack_vector(vector: list[float]) -> bytes:
    """Return the normalized float32 bytes used for integrity fingerprints."""

    normalized = _normalize_vector(vector)
    return struct.pack(f"<{len(normalized)}f", *normalized)


def _faiss_modules() -> tuple[Any, Any]:
    """Load FAISS and NumPy only when review-memory indexing is requested."""

    try:
        faiss = importlib.import_module("faiss")
        numpy = importlib.import_module("numpy")
    except ImportError as exc:
        raise RuntimeError(
            "FAISS review-memory support is unavailable. Install OmniScientist "
            "with the vec extra (pip install -e './cli[vec]')."
        ) from exc
    return faiss, numpy


def _read_json_object(path: Path, *, max_bytes: int = 1024 * 1024) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"expected a regular non-symlink JSON file: {path}")
    raw = path.read_bytes()
    if len(raw) > max_bytes:
        raise ValueError(f"JSON metadata file is unexpectedly large: {path}")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid JSON metadata: {path}") from exc
    if not isinstance(value, dict):
        raise TypeError(f"JSON metadata is not an object: {path}")
    return value


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    encoded = (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    file_descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        with contextlib.suppress(OSError):
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()


def _read_index_snapshot(
    path: Path,
    *,
    validate_artifacts: bool = True,
) -> dict[str, Any]:
    """Resolve one immutable generation and validate its ownership and artifacts."""

    if path.is_symlink() or not path.is_dir():
        raise ValueError("review-memory index path is not a regular directory")
    header = _read_json_object(path / _INDEX_HEADER)
    if header.get("index_owner") != INDEX_OWNER:
        raise ValueError("directory is not an Omni paper-review memory index")
    if (
        header.get("index_format") != _INDEX_FORMAT
        or header.get("similarity") != "cosine"
        or header.get("vectors_normalized") is not True
    ):
        raise ValueError("review-memory FAISS format metadata is invalid")
    generation_name = str(header.get("active_generation") or "")
    if not _GENERATION_RE.fullmatch(generation_name):
        raise ValueError("review-memory active generation name is invalid")

    generations = path / _GENERATIONS_DIR
    if generations.is_symlink() or not generations.is_dir():
        raise ValueError("review-memory generations directory is invalid")
    generation = generations / generation_name
    if generation.is_symlink() or not generation.is_dir():
        raise ValueError("review-memory active generation is missing or unsafe")
    resolved_root = path.resolve()
    resolved_generation = generation.resolve()
    if not _within_root(resolved_generation, resolved_root):
        raise ValueError("review-memory generation escapes the index directory")

    declared_artifacts = header.get("artifacts")
    if not isinstance(declared_artifacts, dict):
        raise TypeError("review-memory artifact metadata is missing")
    artifact_paths: dict[str, Path] = {}
    for filename in (_VECTOR_FILE, _PAPERS_FILE, _REVIEWS_FILE):
        declaration = declared_artifacts.get(filename)
        if not isinstance(declaration, dict):
            raise TypeError(f"review-memory metadata omits {filename}")
        artifact = generation / filename
        if artifact.is_symlink() or not artifact.is_file():
            raise ValueError(f"review-memory artifact is missing or unsafe: {filename}")
        if not _within_root(artifact.resolve(), resolved_generation):
            raise ValueError(f"review-memory artifact escapes its generation: {filename}")
        expected_bytes = int(declaration.get("bytes") or -1)
        if expected_bytes < 0 or artifact.stat().st_size != expected_bytes:
            raise ValueError(f"review-memory artifact size mismatch: {filename}")
        if validate_artifacts:
            expected_hash = str(declaration.get("sha256") or "")
            if not expected_hash or _sha256_file(artifact) != expected_hash:
                raise ValueError(f"review-memory artifact hash mismatch: {filename}")
        artifact_paths[filename] = artifact

    snapshot = dict(header)
    snapshot["_generation_path"] = generation
    snapshot["_artifact_paths"] = artifact_paths
    return snapshot


def _load_paper_records(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    paths = snapshot["_artifact_paths"]
    papers_path = paths[_PAPERS_FILE]
    pack_size = paths[_REVIEWS_FILE].stat().st_size
    records: list[dict[str, Any]] = []
    seen_paper_ids: set[str] = set()
    expected_offset = 0
    with papers_path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                raise ValueError(
                    f"blank line in review-memory paper map at line {line_number}"
                )
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"invalid review-memory paper map line {line_number}"
                ) from exc
            if not isinstance(record, dict):
                raise TypeError(
                    f"review-memory paper map line {line_number} is not an object"
                )
            faiss_id = int(record.get("faiss_id", -1))
            if faiss_id != len(records):
                raise ValueError("review-memory FAISS ids are not contiguous")
            paper_id = _flat_text(record.get("paper_id"))
            if not paper_id or paper_id in seen_paper_ids:
                raise ValueError("review-memory paper ids are missing or duplicated")
            seen_paper_ids.add(paper_id)
            offset = int(record.get("reviews_offset", -1))
            length = int(record.get("reviews_length", -1))
            if offset != expected_offset or length < 0 or offset + length > pack_size:
                raise ValueError("review-memory packet offsets are invalid")
            expected_offset += length
            if int(record.get("reviews_raw_bytes", -1)) < 0:
                raise ValueError("review-memory packet declares an invalid raw size")
            if int(record.get("review_count", 0)) <= 0:
                raise ValueError("review-memory paper has no stored reviews")
            records.append(record)
    expected_count = int(snapshot.get("corpus_paper_count") or 0)
    if len(records) != expected_count:
        raise ValueError("review-memory paper map count does not match index metadata")
    if expected_offset != pack_size:
        raise ValueError("review-memory packet map does not cover reviews.pack")
    return records


def _load_faiss_index(
    snapshot: dict[str, Any],
    *,
    expected_records: int,
) -> Any:
    faiss, _numpy = _faiss_modules()
    try:
        index = faiss.read_index(str(snapshot["_artifact_paths"][_VECTOR_FILE]))
    except Exception as exc:
        raise ValueError("review-memory FAISS index could not be read") from exc
    if type(index).__name__ != "IndexIDMap2":
        raise ValueError("review-memory vector index is not an IndexIDMap2")
    try:
        inner = faiss.downcast_index(index.index)
    except Exception as exc:
        raise ValueError("review-memory FAISS inner index is invalid") from exc
    if type(inner).__name__ != "IndexFlatIP":
        raise ValueError("review-memory vector index is not exact IndexFlatIP")
    dimension = int(snapshot.get("embedding_dimension") or 0)
    if dimension <= 0 or int(index.d) != dimension:
        raise ValueError("review-memory FAISS dimension does not match metadata")
    if int(index.ntotal) != expected_records:
        raise ValueError("review-memory FAISS count does not match the paper map")
    try:
        stored_ids = faiss.vector_to_array(index.id_map)
    except Exception as exc:
        raise ValueError("review-memory FAISS id map could not be read") from exc
    if [int(item) for item in stored_ids.tolist()] != list(range(expected_records)):
        raise ValueError("review-memory FAISS id map is not canonical")
    return index


def _write_paper_record(handle: Any, record: dict[str, Any]) -> None:
    encoded = (
        json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    )
    handle.write(encoded)


async def build_review_index(
    manifest_path: str | Path,
    index_path: str | Path,
    *,
    embedder: Embedder,
    embedding_model: str,
    embedding_space_id: str = "",
    batch_size: int = 32,
    rebuild: bool = False,
    limit: int | None = None,
    progress: Callable[[dict[str, Any]], None] | None = None,
    allowed_data_root: str | Path | None = None,
) -> dict[str, Any]:
    """Build an atomic FAISS review-memory generation.

    Every invocation creates a complete immutable generation. Existing generations
    are never edited and the root index.json pointer changes only after validation.
    """

    manifest = Path(manifest_path).expanduser().resolve()
    raw_destination = Path(index_path).expanduser()
    if raw_destination.is_symlink():
        raise ValueError(f"refusing symlink index path: {raw_destination}")
    destination = raw_destination.resolve()
    model = _flat_text(embedding_model)
    space_id = _flat_text(embedding_space_id or getattr(embedder, "space_id", ""))
    if not manifest.is_file():
        raise FileNotFoundError(manifest)
    if not _manifest_has_record(manifest):
        raise ValueError("source manifest contains no paper records")
    if destination == manifest:
        raise ValueError("index path must differ from the source manifest")
    if not model:
        raise ValueError("embedding_model is required for index compatibility checks")
    if not space_id:
        raise ValueError(
            "embedding_space_id is required to bind build and query embedding spaces"
        )
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    if limit is not None and limit < 1:
        raise ValueError("limit must be positive when supplied")
    data_root = (
        Path(allowed_data_root).expanduser().resolve()
        if allowed_data_root is not None
        else manifest.parent.parent.resolve()
    )
    if not data_root.is_dir():
        raise ValueError(f"allowed data root is not a directory: {data_root}")
    destination.parent.mkdir(parents=True, exist_ok=True)

    with _BuildLock(destination):
        _validate_owned_destination(destination)
        if destination.exists():
            current = _read_index_snapshot(destination, validate_artifacts=True)
            incompatible = (
                current.get("schema_version") != INDEX_SCHEMA_VERSION
                or current.get("embedding_text_policy") != EMBEDDING_TEXT_POLICY
                or current.get("review_text_policy") != REVIEW_TEXT_POLICY
                or str(current.get("embedding_model") or "") != model
                or str(current.get("embedding_space_id") or "") != space_id
            )
            if incompatible and not rebuild:
                raise ValueError(
                    "existing review index uses an incompatible schema, policy, "
                    "model, or embedding space; rebuild it"
                )
            return await _build_review_index_file(
                manifest,
                destination,
                embedder=embedder,
                embedding_model=model,
                embedding_space_id=space_id,
                batch_size=batch_size,
                limit=limit,
                progress=progress,
                allowed_data_root=data_root,
                rebuild=rebuild,
            )

        temporary = Path(
            tempfile.mkdtemp(
                dir=destination.parent,
                prefix=f".{destination.name}.building-",
            )
        )
        try:
            result = await _build_review_index_file(
                manifest,
                temporary,
                embedder=embedder,
                embedding_model=model,
                embedding_space_id=space_id,
                batch_size=batch_size,
                limit=limit,
                progress=progress,
                allowed_data_root=data_root,
                rebuild=rebuild,
            )
            os.replace(temporary, destination)
            return {**result, "index_path": str(destination)}
        finally:
            if temporary.exists() and not temporary.is_symlink():
                shutil.rmtree(temporary)


async def _build_review_index_file(
    manifest: Path,
    destination: Path,
    *,
    embedder: Embedder,
    embedding_model: str,
    embedding_space_id: str,
    batch_size: int,
    limit: int | None,
    progress: Callable[[dict[str, Any]], None] | None,
    allowed_data_root: Path,
    rebuild: bool,
) -> dict[str, Any]:
    """Build one complete generation while the caller owns the build lock."""

    started = time.monotonic()
    manifest_hash = _sha256_file(manifest)
    generation_name = f"gen-{uuid.uuid4().hex}"
    generations = destination / _GENERATIONS_DIR
    if generations.exists() and (generations.is_symlink() or not generations.is_dir()):
        raise ValueError("review-memory generations path is unsafe")
    generations.mkdir(parents=True, exist_ok=True)
    building_generation = generations / f".building-{uuid.uuid4().hex}"
    final_generation = generations / generation_name
    building_generation.mkdir()

    vector_path = building_generation / _VECTOR_FILE
    papers_path = building_generation / _PAPERS_FILE
    reviews_path = building_generation / _REVIEWS_FILE
    indexed = 0
    seen = 0
    review_count = 0
    first_conference = ""
    expected_dimension: int | None = None
    faiss_index: Any | None = None
    pending: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    corpus_content_digest = hashlib.sha256()

    try:
        faiss, numpy = _faiss_modules()
        with (
            papers_path.open("x", encoding="utf-8", newline="\n") as papers_handle,
            reviews_path.open("xb") as reviews_handle,
        ):

            async def flush() -> None:
                nonlocal indexed, expected_dimension, faiss_index
                if not pending:
                    return
                try:
                    vectors = await embedder(
                        [str(record["retrieval_text"]) for record in pending]
                    )
                except Exception as exc:  # noqa: BLE001 - provider details stay private
                    raise RuntimeError(
                        f"embedding batch failed: {_safe_embedding_error(exc)}"
                    ) from None
                if len(vectors) != len(pending):
                    raise ValueError(
                        "embedding provider returned a different number of vectors "
                        "than requested"
                    )
                normalized = [_normalize_vector(vector) for vector in vectors]
                dimensions = {len(vector) for vector in normalized}
                if len(dimensions) != 1:
                    raise ValueError(
                        "embedding provider returned inconsistent vector dimensions"
                    )
                dimension = dimensions.pop()
                if expected_dimension is None:
                    expected_dimension = dimension
                    faiss_index = faiss.IndexIDMap2(faiss.IndexFlatIP(dimension))
                elif dimension != expected_dimension:
                    raise ValueError(
                        f"embedding dimension changed from {expected_dimension} "
                        f"to {dimension}"
                    )
                matrix = numpy.asarray(normalized, dtype="float32")
                norms = numpy.linalg.norm(matrix, axis=1, keepdims=True)
                if (
                    not numpy.isfinite(matrix).all()
                    or not numpy.isfinite(norms).all()
                    or bool((norms <= 0).any())
                ):
                    raise ValueError("embedding batch cannot be normalized for FAISS")
                matrix = numpy.ascontiguousarray(matrix / norms, dtype="float32")
                ids = numpy.arange(
                    indexed,
                    indexed + len(pending),
                    dtype="int64",
                )
                faiss_index.add_with_ids(matrix, ids)

                for offset_in_batch, record in enumerate(pending):
                    faiss_id = indexed + offset_in_batch
                    blob = bytes(record["reviews_blob"])
                    packet_offset = reviews_handle.tell()
                    reviews_handle.write(blob)
                    embedding_bytes = matrix[offset_in_batch].tobytes()
                    stored = {
                        "faiss_id": faiss_id,
                        "paper_id": str(record["paper_id"]),
                        "title": str(record["title"]),
                        "normalized_title": str(record["normalized_title"]),
                        "abstract": str(record["abstract"]),
                        "retrieval_hash": str(record["retrieval_hash"]),
                        "paper_sha256": str(record["paper_sha256"]),
                        "reviews_json_sha256": str(record["reviews_json_sha256"]),
                        "source_line": int(record["source_line"]),
                        "review_count": int(record["review_count"]),
                        "reviews_offset": packet_offset,
                        "reviews_length": len(blob),
                        "reviews_blob_sha256": _sha256_bytes(blob),
                        "reviews_raw_bytes": int(record["reviews_raw_bytes"]),
                        "reviews_raw_sha256": str(record["reviews_raw_sha256"]),
                        "embedding_sha256": _sha256_bytes(embedding_bytes),
                    }
                    _write_paper_record(papers_handle, stored)
                    corpus_content_digest.update(
                        (
                            f"{stored['paper_id']}\0{stored['retrieval_hash']}\0"
                            f"{stored['paper_sha256']}\0"
                            f"{stored['reviews_json_sha256']}\0"
                            f"{stored['reviews_raw_sha256']}\0"
                            f"{stored['embedding_sha256']}\n"
                        ).encode()
                    )
                indexed += len(pending)
                pending.clear()
                if progress is not None:
                    progress(
                        {
                            "seen": seen,
                            "indexed": indexed,
                            "skipped": 0,
                            "reviews": review_count,
                        }
                    )

            for source_line, row in _iter_jsonl(manifest):
                if limit is not None and seen >= limit:
                    break
                record = _manifest_record(
                    row,
                    manifest_path=manifest,
                    source_line=source_line,
                    allowed_data_root=allowed_data_root,
                )
                paper_id = str(record["paper_id"])
                if paper_id in seen_ids:
                    raise ValueError(
                        f"duplicate paper id {paper_id} on manifest line {source_line}"
                    )
                seen_ids.add(paper_id)
                seen += 1
                review_count += int(record["review_count"])
                first_conference = first_conference or str(record["conference"])
                pending.append(record)
                if len(pending) >= batch_size:
                    await flush()
            await flush()
            papers_handle.flush()
            reviews_handle.flush()
            os.fsync(papers_handle.fileno())
            os.fsync(reviews_handle.fileno())

        if _sha256_file(manifest) != manifest_hash:
            raise RuntimeError("source manifest changed while the index was building")
        if indexed <= 0 or expected_dimension is None or faiss_index is None:
            raise ValueError("source manifest produced no indexable papers or embeddings")
        faiss.write_index(faiss_index, str(vector_path))
        # faiss closes its own handle, so durability needs a reopen. It must be a
        # writable one: on Windows ``os.fsync`` is ``_commit``, which refuses a
        # read-only descriptor with EBADF, and every build here would fail.
        with vector_path.open("rb+") as vector_handle:
            os.fsync(vector_handle.fileno())

        corpus_content_sha256 = corpus_content_digest.hexdigest()
        fingerprint = hashlib.sha256(
            (
                f"{INDEX_SCHEMA_VERSION}:{corpus_content_sha256}:{embedding_model}:"
                f"{embedding_space_id}:{EMBEDDING_TEXT_POLICY}:"
                f"{REVIEW_TEXT_POLICY}:{expected_dimension}:{indexed}:{review_count}"
            ).encode()
        ).hexdigest()
        artifacts = {
            filename: {
                "bytes": (building_generation / filename).stat().st_size,
                "sha256": _sha256_file(building_generation / filename),
            }
            for filename in (_VECTOR_FILE, _PAPERS_FILE, _REVIEWS_FILE)
        }
        header = {
            "index_owner": INDEX_OWNER,
            "schema_version": INDEX_SCHEMA_VERSION,
            "status": "ready",
            "active_generation": generation_name,
            "index_format": _INDEX_FORMAT,
            "similarity": "cosine",
            "vectors_normalized": True,
            "embedding_model": embedding_model,
            "embedding_space_id": embedding_space_id,
            "embedding_dimension": expected_dimension,
            "embedding_text_policy": EMBEDDING_TEXT_POLICY,
            "review_text_policy": REVIEW_TEXT_POLICY,
            "manifest_name": manifest.name,
            "manifest_sha256": manifest_hash,
            "corpus_paper_count": indexed,
            "corpus_review_count": review_count,
            "corpus_venue": first_conference,
            "corpus_content_sha256": corpus_content_sha256,
            "index_fingerprint": fingerprint,
            "completed_at_unix": int(time.time()),
            "artifacts": artifacts,
        }
        candidate_snapshot = {
            **header,
            "_artifact_paths": {
                filename: building_generation / filename
                for filename in (_VECTOR_FILE, _PAPERS_FILE, _REVIEWS_FILE)
            },
        }
        candidate_records = _load_paper_records(candidate_snapshot)
        _load_faiss_index(
            candidate_snapshot,
            expected_records=len(candidate_records),
        )
        os.replace(building_generation, final_generation)
        _atomic_write_json(destination / _INDEX_HEADER, header)
        _read_index_snapshot(destination, validate_artifacts=True)
    finally:
        if building_generation.exists() and not building_generation.is_symlink():
            shutil.rmtree(building_generation)

    return {
        "status": "ok",
        "index_path": str(destination),
        "schema_version": INDEX_SCHEMA_VERSION,
        "active_generation": generation_name,
        "index_format": _INDEX_FORMAT,
        "retrieval_mode": "faiss",
        "manifest_path": str(manifest),
        "manifest_sha256": manifest_hash,
        "corpus_content_sha256": corpus_content_sha256,
        "embedding_model": embedding_model,
        "embedding_space_id": embedding_space_id,
        "embedding_dimension": expected_dimension,
        "embedding_text_policy": EMBEDDING_TEXT_POLICY,
        "review_text_policy": REVIEW_TEXT_POLICY,
        "papers_seen": seen,
        "papers_embedded": indexed,
        "papers_unchanged": 0,
        "incremental_reuse": False,
        "rebuild_requested": bool(rebuild),
        "paper_count": indexed,
        "review_count": review_count,
        "corpus_venue": first_conference,
        "index_fingerprint": fingerprint,
        "artifacts": artifacts,
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }


def inspect_review_index(index_path: str | Path) -> dict[str, Any]:
    """Return key-free metadata for a FAISS review-memory index."""

    raw_path = Path(index_path).expanduser()
    if raw_path.is_symlink():
        return {
            "status": "invalid",
            "index_path": str(raw_path),
            "error": "index path is a symlink",
        }
    path = raw_path.resolve()
    if not path.exists():
        return {"status": "missing", "index_path": str(path)}
    try:
        snapshot = _read_index_snapshot(path, validate_artifacts=True)
        records = _load_paper_records(snapshot)
        _load_faiss_index(snapshot, expected_records=len(records))
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        return {
            "status": "invalid",
            "index_path": str(path),
            "error": _flat_text(exc),
        }
    return {
        "status": str(snapshot.get("status") or "unknown"),
        "index_path": str(path),
        "schema_version": str(snapshot.get("schema_version") or ""),
        "active_generation": str(snapshot.get("active_generation") or ""),
        "index_format": str(snapshot.get("index_format") or ""),
        "retrieval_mode": "faiss",
        "embedding_model": str(snapshot.get("embedding_model") or ""),
        "embedding_space_id": str(snapshot.get("embedding_space_id") or ""),
        "embedding_dimension": int(snapshot.get("embedding_dimension") or 0),
        "embedding_text_policy": str(snapshot.get("embedding_text_policy") or ""),
        "review_text_policy": str(snapshot.get("review_text_policy") or ""),
        "corpus_venue": str(snapshot.get("corpus_venue") or ""),
        "paper_count": len(records),
        "review_count": sum(int(record["review_count"]) for record in records),
        "index_fingerprint": str(snapshot.get("index_fingerprint") or ""),
        "artifacts": dict(snapshot.get("artifacts") or {}),
    }


def _semantic_candidates(
    snapshot: dict[str, Any],
    records: list[dict[str, Any]],
    query_vector: list[float],
    *,
    dimension: int,
    limit: int,
    excluded_title: str,
    excluded_hash: str,
    excluded_paper_id: str,
) -> list[dict[str, Any]]:
    faiss_index = _load_faiss_index(snapshot, expected_records=len(records))
    _faiss, numpy = _faiss_modules()
    normalized = _normalize_vector(query_vector)
    if len(normalized) != dimension:
        raise ValueError(
            f"query embedding dimension {len(normalized)} does not match "
            f"index dimension {dimension}"
        )
    query = numpy.asarray([normalized], dtype="float32")
    norms = numpy.linalg.norm(query, axis=1, keepdims=True)
    if not numpy.isfinite(norms).all() or bool((norms <= 0).any()):
        raise ValueError("query embedding cannot be normalized for FAISS")
    query = numpy.ascontiguousarray(query / norms, dtype="float32")
    search_k = min(len(records), max(limit * 4, 64))
    scores, ids = faiss_index.search(query, search_k)
    candidates: list[dict[str, Any]] = []
    for raw_score, raw_id in zip(scores[0].tolist(), ids[0].tolist(), strict=True):
        faiss_id = int(raw_id)
        similarity = float(raw_score)
        if faiss_id < 0:
            continue
        if faiss_id >= len(records) or not math.isfinite(similarity):
            raise ValueError("FAISS returned an invalid review-memory result")
        record = records[faiss_id]
        if excluded_paper_id and str(record["paper_id"]) == excluded_paper_id:
            continue
        if excluded_hash and str(record["retrieval_hash"]) == excluded_hash:
            continue
        if excluded_title and str(record["normalized_title"]) == excluded_title:
            continue
        candidates.append(
            {
                **record,
                "similarity": round(similarity, 6),
            }
        )
        if len(candidates) >= limit:
            break
    return candidates


def _review_for_author_guidance(review: dict[str, Any]) -> dict[str, Any]:
    content = review.get("content")
    content = content if isinstance(content, dict) else {}
    textual: dict[str, Any] = {}
    for field, value in content.items():
        if str(field).casefold() not in _AUTHOR_GUIDANCE_FIELDS:
            continue
        if isinstance(value, str):
            cleaned: Any = _redact_historical_review_text(value)
        elif isinstance(value, list):
            cleaned = [
                redacted
                for item in value
                if (redacted := _redact_historical_review_text(item))
            ]
        else:
            continue
        if cleaned:
            textual[str(field)] = cleaned
    return {"textual_review_fields": textual}


def _decompress_review_packet(
    blob: bytes,
    *,
    expected_bytes: int,
    expected_sha256: str,
) -> Any:
    if expected_bytes < 0 or expected_bytes > MAX_REVIEW_PACKET_BYTES:
        raise ValueError("stored review packet declares an unsafe size")
    decompressor = zlib.decompressobj()
    raw = decompressor.decompress(blob, MAX_REVIEW_PACKET_BYTES + 1)
    if len(raw) > MAX_REVIEW_PACKET_BYTES or decompressor.unconsumed_tail:
        raise ValueError("stored review packet exceeds its safe size limit")
    remaining = MAX_REVIEW_PACKET_BYTES + 1 - len(raw)
    raw += decompressor.flush(remaining)
    if (
        len(raw) > MAX_REVIEW_PACKET_BYTES
        or not decompressor.eof
        or decompressor.unused_data
    ):
        raise ValueError("stored review packet is truncated or has trailing data")
    if len(raw) != expected_bytes or _sha256_bytes(raw) != expected_sha256:
        raise ValueError("stored review packet failed its integrity check")
    return json.loads(raw)


def _load_review_packets(
    snapshot: dict[str, Any],
    candidates: list[dict[str, Any]],
    *,
    top_k: int,
) -> tuple[list[dict[str, Any]], list[str]]:
    packets: list[dict[str, Any]] = []
    warnings: list[str] = []
    reviews_path = snapshot["_artifact_paths"][_REVIEWS_FILE]
    with reviews_path.open("rb") as handle:
        for candidate in candidates:
            try:
                offset = int(candidate["reviews_offset"])
                length = int(candidate["reviews_length"])
                handle.seek(offset)
                blob = handle.read(length)
                if (
                    len(blob) != length
                    or _sha256_bytes(blob)
                    != str(candidate.get("reviews_blob_sha256") or "")
                ):
                    raise ValueError("stored compressed review packet is invalid")
                reviews = _decompress_review_packet(
                    blob,
                    expected_bytes=int(candidate["reviews_raw_bytes"]),
                    expected_sha256=str(candidate["reviews_raw_sha256"]),
                )
            except (
                KeyError,
                OSError,
                TypeError,
                ValueError,
                zlib.error,
                json.JSONDecodeError,
            ) as exc:
                warnings.append(
                    "Could not read a historical review packet because its stored "
                    f"content was invalid ({type(exc).__name__})."
                )
                continue
            if not isinstance(reviews, list):
                warnings.append(
                    f"Historical review packet for {candidate['paper_id']} is malformed."
                )
                continue
            guidance_reviews = [
                _review_for_author_guidance(review)
                for review in reviews
                if isinstance(review, dict)
            ]
            guidance_reviews = [
                review
                for review in guidance_reviews
                if review["textual_review_fields"]
            ]
            if not guidance_reviews:
                continue
            packets.append({**candidate, "official_reviews": guidance_reviews})
            if len(packets) >= top_k:
                break
    return packets, warnings


def _read_ready_index_header(path: Path) -> dict[str, Any]:
    return _read_index_snapshot(path, validate_artifacts=False)


def _safe_embedding_error(
    exc: Exception,
    *,
    include_validation_detail: bool = False,
) -> str:
    code = str(getattr(exc, "code", "") or "")
    status = getattr(exc, "http_status", None)
    if code == "embedding_http_error" and isinstance(status, int):
        return f"embedding endpoint returned HTTP {status}"
    safe_categories = {
        "embedding_timeout": "embedding request timed out",
        "embedding_transport_error": "embedding transport failed",
        "embedding_invalid_response": "embedding endpoint returned an invalid response",
    }
    if code in safe_categories:
        return safe_categories[code]
    if isinstance(exc, NotImplementedError):
        return "embedding runtime is unavailable"
    if include_validation_detail and isinstance(exc, ValueError):
        return _flat_text(exc)
    return f"embedding request failed ({type(exc).__name__})"


def _retrieve_snapshot(
    path: Path,
    *,
    query_vector: list[float] | None,
    embedding_error: str,
    expected_fingerprint: str,
    requested_k: int,
    excluded_title: str,
    excluded_hash: str,
    excluded_paper_id: str,
) -> dict[str, Any]:
    """Read one immutable FAISS generation and its review packets."""

    snapshot = _read_index_snapshot(path, validate_artifacts=True)
    if snapshot.get("schema_version") != INDEX_SCHEMA_VERSION:
        return {
            "unavailable_code": "review_memory_index_incompatible",
            "warning": "Historical-review index schema is incompatible; rebuild it.",
        }
    if (
        snapshot.get("embedding_text_policy") != EMBEDDING_TEXT_POLICY
        or snapshot.get("review_text_policy") != REVIEW_TEXT_POLICY
    ):
        return {
            "unavailable_code": "review_memory_index_incompatible",
            "warning": "Historical-review index policies are outdated; rebuild it.",
        }
    if snapshot.get("status") != "ready":
        return {
            "unavailable_code": "review_memory_index_not_ready",
            "warning": "Historical-review index is incomplete; rebuild it.",
        }
    fingerprint = str(snapshot.get("index_fingerprint") or "")
    if expected_fingerprint and fingerprint != expected_fingerprint:
        return {
            "unavailable_code": "review_memory_index_changed",
            "warning": (
                "Historical-review index changed during retrieval; rerun the review "
                "to use one consistent index snapshot."
            ),
        }
    if embedding_error or query_vector is None:
        return {
            "unavailable_code": "review_memory_embedding_unavailable",
            "warning": (
                "Semantic historical-review retrieval is unavailable: "
                f"{embedding_error or 'query embedding was not produced'}"
            ),
        }

    records = _load_paper_records(snapshot)
    candidates = _semantic_candidates(
        snapshot,
        records,
        query_vector,
        dimension=int(snapshot.get("embedding_dimension") or 0),
        limit=max(requested_k * 3, 20),
        excluded_title=excluded_title,
        excluded_hash=excluded_hash,
        excluded_paper_id=excluded_paper_id,
    )
    packets, packet_warnings = _load_review_packets(
        snapshot,
        candidates,
        top_k=requested_k,
    )
    return {
        "meta": snapshot,
        "retrieval_mode": "faiss",
        "packets": packets,
        "warnings": packet_warnings,
    }


async def retrieve_review_memory(
    index_path: str | Path,
    *,
    embedder: Embedder,
    structure: dict[str, Any],
    top_k: int = DEFAULT_TOP_K,
    embedding_model: str = "",
    embedding_space_id: str = "",
) -> dict[str, Any]:
    """Retrieve whole redacted reviews using exact FAISS cosine similarity."""

    raw_path = Path(index_path).expanduser()
    if raw_path.is_symlink():
        return _unavailable_result(
            raw_path,
            code="review_memory_index_invalid",
            warning="Historical-review index path must not be a symlink.",
        )
    path = raw_path.resolve()
    requested_k = max(1, min(int(top_k), 10))
    if not path.is_dir():
        return _unavailable_result(
            path,
            code="review_memory_index_missing",
            warning=(
                "Historical-review RAG was requested, but its index directory "
                "does not exist."
            ),
        )
    title = _flat_text(structure.get("title"))
    abstract = _flat_text(structure.get("abstract"))
    if not title and not abstract:
        return _unavailable_result(
            path,
            code="review_memory_query_insufficient",
            warning=(
                "Historical-review retrieval needs a paper title or abstract, but "
                "neither was available."
            ),
        )
    query = paper_embedding_text(title, abstract)
    excluded_title = _normalized_title(structure.get("title"))
    excluded_hash = _sha256_bytes(query.encode("utf-8"))
    excluded_paper_id = _flat_text(
        structure.get("paper_id")
        or structure.get("source_paper_id")
        or structure.get("openreview_forum_id")
    )
    try:
        header = await asyncio.to_thread(_read_ready_index_header, path)
    except (OSError, TypeError, ValueError):
        return _unavailable_result(
            path,
            code="review_memory_index_invalid",
            warning="Historical-review index could not be read or is not owned by Omni.",
        )
    if header.get("schema_version") != INDEX_SCHEMA_VERSION:
        return _unavailable_result(
            path,
            code="review_memory_index_incompatible",
            warning="Historical-review index schema is incompatible; rebuild it.",
        )
    if (
        header.get("embedding_text_policy") != EMBEDDING_TEXT_POLICY
        or header.get("review_text_policy") != REVIEW_TEXT_POLICY
    ):
        return _unavailable_result(
            path,
            code="review_memory_index_incompatible",
            warning="Historical-review index policies are outdated; rebuild it.",
        )
    if header.get("status") != "ready":
        return _unavailable_result(
            path,
            code="review_memory_index_not_ready",
            warning="Historical-review index is incomplete; rebuild it.",
        )

    index_model = str(header.get("embedding_model") or "")
    configured_model = _flat_text(embedding_model)
    index_space_id = str(header.get("embedding_space_id") or "")
    configured_space_id = _flat_text(
        embedding_space_id or getattr(embedder, "space_id", "")
    )
    embedding_error = ""
    query_vector: list[float] | None = None
    if not configured_space_id:
        embedding_error = "configured embedding space is unavailable"
    elif index_space_id != configured_space_id:
        embedding_error = (
            "configured embedding service/model space does not match the index"
        )
    elif configured_model and index_model and configured_model != index_model:
        embedding_error = (
            f"configured embedding model {configured_model} does not match "
            f"index model {index_model}"
        )
    else:
        try:
            vectors = await embedder([query])
        except NotImplementedError as exc:
            embedding_error = _safe_embedding_error(exc)
        except Exception as exc:  # noqa: BLE001 - do not expose provider details
            embedding_error = _safe_embedding_error(exc)
        else:
            try:
                if len(vectors) != 1:
                    raise ValueError(
                        "embedding provider did not return exactly one query vector"
                    )
                query_vector = [float(value) for value in vectors[0]]
                expected_dimension = int(header.get("embedding_dimension") or 0)
                if len(query_vector) != expected_dimension:
                    raise ValueError(
                        f"query embedding dimension {len(query_vector)} does not match "
                        f"index dimension {expected_dimension}"
                    )
            except (TypeError, ValueError) as exc:
                embedding_error = _safe_embedding_error(
                    exc,
                    include_validation_detail=True,
                )

    try:
        snapshot = await asyncio.to_thread(
            _retrieve_snapshot,
            path,
            query_vector=query_vector,
            embedding_error=embedding_error,
            expected_fingerprint=str(header.get("index_fingerprint") or ""),
            requested_k=requested_k,
            excluded_title=excluded_title,
            excluded_hash=excluded_hash,
            excluded_paper_id=excluded_paper_id,
        )
    except RuntimeError as exc:
        return _unavailable_result(
            path,
            code="review_memory_faiss_unavailable",
            warning=_flat_text(exc),
        )
    except (OSError, TypeError, ValueError):
        return _unavailable_result(
            path,
            code="review_memory_index_invalid",
            warning="Historical-review FAISS index could not be validated or read.",
        )
    unavailable_code = str(snapshot.get("unavailable_code") or "")
    if unavailable_code:
        return _unavailable_result(
            path,
            code=unavailable_code,
            warning=str(snapshot.get("warning") or "Historical-review retrieval failed."),
        )

    meta = snapshot["meta"]
    packets = list(snapshot["packets"])
    warnings = list(snapshot["warnings"])
    matches = [
        {
            "paper_id": packet["paper_id"],
            "title": packet["title"],
            "similarity": packet["similarity"],
            "review_count": len(packet["official_reviews"]),
        }
        for packet in packets
    ]
    if len(packets) < requested_k:
        warnings.append(
            f"Historical-review retrieval returned {len(packets)} complete paper packets "
            f"for the requested top {requested_k}."
        )
    status = "ok" if len(packets) == requested_k else "partial"
    code = (
        "review_memory_retrieved"
        if status == "ok"
        else "review_memory_retrieved_with_limits"
    )
    return {
        "status": status,
        "outcome": {"code": code},
        "index_path": str(path),
        "index_fingerprint": str(meta.get("index_fingerprint") or ""),
        "active_generation": str(meta.get("active_generation") or ""),
        "index_format": str(meta.get("index_format") or ""),
        "corpus_venue": str(meta.get("corpus_venue") or ""),
        "corpus_paper_count": int(meta.get("corpus_paper_count") or 0),
        "corpus_review_count": int(meta.get("corpus_review_count") or 0),
        "embedding_model": str(meta.get("embedding_model") or ""),
        "embedding_space_id": str(meta.get("embedding_space_id") or ""),
        "retrieval_mode": "faiss",
        "requested_top_k": requested_k,
        "matched_paper_count": len(packets),
        "review_count": sum(len(packet["official_reviews"]) for packet in packets),
        "matches": matches,
        "warnings": warnings,
        "_review_packets": packets,
        "evidence_boundary": (
            "Historical reviews reveal concerns raised for other papers. They are not "
            "evidence of a flaw in the current manuscript, an official rubric, prior art, "
            "or a score/decision prior. Verify every concern against the current paper."
        ),
    }


def _unavailable_result(path: Path, *, code: str, warning: str) -> dict[str, Any]:
    embedding_problem = code == "review_memory_embedding_unavailable"
    return {
        "status": "unavailable",
        "outcome": {"code": code},
        "index_path": str(path),
        "retrieval_mode": "none",
        "matched_paper_count": 0,
        "review_count": 0,
        "matches": [],
        "warnings": [warning],
        "_review_packets": [],
        "setup_command": (
            "omni config embeddings --help"
            if embedding_problem
            else review_index_setup_command(
                rebuild=code == "review_memory_index_incompatible"
            )
        ),
        "next_actions": (
            [
                (
                    "Configure the exact embedding model and space used by the "
                    "FAISS index (the production corpus uses local SPECTER2 "
                    "proximity), then rerun paper-review."
                )
            ]
            if embedding_problem
            else ["Build or rebuild the FAISS historical-review index."]
        ),
    }


def public_review_memory(result: dict[str, Any]) -> dict[str, Any]:
    """Strip review bodies and filesystem-only internals from the public result."""

    return {
        key: value
        for key, value in result.items()
        if key not in {"_review_packets"}
    }


__all__ = [
    "DEFAULT_TOP_K",
    "EMBEDDING_TEXT_POLICY",
    "INDEX_SCHEMA_VERSION",
    "build_review_index",
    "inspect_review_index",
    "paper_embedding_text",
    "public_review_memory",
    "retrieve_review_memory",
]
