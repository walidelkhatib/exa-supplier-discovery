"""Supplier discovery on top of Exa's /search endpoint.

One Exa call does both retrieval and extraction:
  - `contents={"highlights": True}` so the extraction reads page text, not just titles.
  - `output_schema` + `system_prompt` so Exa returns the supplier fields as JSON,
    with field-level citations in `output.grounding`.

Exa's grounding gives a source URL per field but no snippet, so the schema also
asks for a short verbatim quote per field. We then check that quote against the
text Exa returned (highlights). A field is only shown when it has a value, a
quote, and a source URL; otherwise it becomes "Not verified".
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlparse

from dotenv import load_dotenv
from exa_py import Exa

load_dotenv()

NOT_VERIFIED = "Not verified"
TOP_N = 5

# Fallback cost per request (USD, up to 10 results) from https://exa.ai/pricing,
# used only if the API response doesn't report costDollars. Labeled "estimated".
ESTIMATED_COST_PER_REQUEST = {"lite": 0.004, "auto": 0.007, "deep-lite": 0.012, "deep": 0.012}

# Compact schema: Exa caps outputSchema at 10 properties total and 2 nesting levels.
# This uses 9 (suppliers + 8 item fields).
OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "suppliers": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "website": {"type": "string"},
                    "location": {"type": "string"},
                    "location_quote": {"type": "string"},
                    "capabilities": {"type": "string"},
                    "capabilities_quote": {"type": "string"},
                    "certifications": {"type": "string"},
                    "certifications_quote": {"type": "string"},
                },
                "required": [
                    "name", "website",
                    "location", "location_quote",
                    "capabilities", "capabilities_quote",
                    "certifications", "certifications_quote",
                ],
            },
        }
    },
    "required": ["suppliers"],
}

# Keep/drop and verification rules live here, not in the query (per Exa guidance).
SYSTEM_PROMPT = f"""You extract automotive supplier facts for a procurement team. Every claim must be backed by the retrieved page text.

Rules:
- Include only companies that themselves manufacture or supply the requested parts or services. Drop directories, marketplaces, news sites, trade associations, and OEMs that are only customers.
- Return at most {TOP_N} suppliers, one entry per company (no duplicate domains), best match first.
- website: the company's own homepage URL (scheme + domain).
- location, capabilities, certifications: only state what the retrieved text explicitly says. If the text does not support a field, set it to exactly "{NOT_VERIFIED}" and set its quote to "".
- certifications: list only certification names the page mentions (e.g. IATF 16949, ISO 14001). Do not infer a certification from industry or customers.
- Each *_quote field: copy one contiguous sentence or phrase verbatim from the retrieved text. Do not paraphrase, join passages, or use "...". Never invent a quote.
- Each field's value must be supported by its quote alone. Drop any part of the value (e.g. a certification name or a city) that the quote does not state."""

OBJECTIVE = (
    "Find the websites of companies that manufacture or supply the requested automotive parts. "
    "Rank the suppliers' own company pages (capabilities, about, quality/certification pages) first; "
    "exclude supplier directories, marketplaces, news articles and job listings. "
    "Pull each supplier's location, manufacturing capabilities, and any certifications mentioned."
)

FIELDS = ("location", "capabilities", "certifications")


@dataclass
class Evidence:
    value: str
    url: Optional[str] = None
    snippet: Optional[str] = None
    confidence: Optional[str] = None
    quote_matched: bool = False  # quote found verbatim in Exa's returned highlights

    @property
    def verified(self) -> bool:
        return self.value != NOT_VERIFIED


@dataclass
class Supplier:
    name: str
    website: str
    website_source: Optional[str]
    fields: dict[str, Evidence]


@dataclass
class SearchRun:
    query: str
    mode: str
    suppliers: list[Supplier] = field(default_factory=list)
    result_urls: list[str] = field(default_factory=list)
    latency_s: float = 0.0
    api_search_time_s: Optional[float] = None
    cost_usd: Optional[float] = None
    cost_source: str = "reported"  # "reported" or "estimated"
    error: Optional[str] = None


def get_client() -> Exa:
    key = os.getenv("EXA_API_KEY")
    if not key:
        raise RuntimeError("EXA_API_KEY is not set. Copy .env.example to .env and add your key.")
    return Exa(api_key=key)


def domain_of(url: str) -> str:
    if not url:
        return ""
    if "://" not in url:
        url = "https://" + url
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


# Hosts that are never a supplier's own website.
NOT_OWN_SITE = ("linkedin.com", "facebook.com", "instagram.com", "x.com", "twitter.com",
                "youtube.com", "wikipedia.org", "bloomberg.com", "zoominfo.com", "thomasnet.com")


def own_site_domain(url: str) -> Optional[str]:
    """Domain of a plausible company homepage, or None ('Not verified', social, directory)."""
    dom = domain_of(url)
    if not re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)+", dom):
        return None
    if any(dom == h or dom.endswith("." + h) for h in NOT_OWN_SITE):
        return None
    return dom


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", text.lower())).strip()


CERT_RE = re.compile(r"\b(IATF|ISO|AS|TS|QS|VDA)[\s-]?(\d{3,5})(?::\d{4})?", re.I)


def _filter_certs(value: str, quote: str) -> str:
    """Keep only standards (IATF 16949, ISO 14001, ...) that the quote itself names.

    The prompt asks for this too, but models sometimes add a standard from another
    passage, so it is enforced in code. Values with no recognizable standard pass through.
    """
    # Match on the standard's number, so "ISO 14001 and 45001" supports ISO 45001.
    numbers_in_quote = set(re.findall(r"\b\d{3,5}\b", quote))
    found = [m.group(0) for m in CERT_RE.finditer(value)]
    kept = [c for c in found if CERT_RE.match(c).group(2) in numbers_in_quote]
    if len(kept) == len(found):
        return value  # all supported: keep the original wording and its qualifiers
    return ", ".join(dict.fromkeys(kept)) if kept else NOT_VERIFIED


def _grounding_map(output) -> dict[str, tuple[list[str], str]]:
    """Map field path (e.g. 'suppliers[0].location') -> (citation urls, confidence)."""
    out: dict[str, tuple[list[str], str]] = {}
    for g in getattr(output, "grounding", None) or []:
        out[g.field] = ([c.url for c in g.citations], g.confidence)
    return out


def _cited(gmap, i: int, name: str) -> tuple[list[str], Optional[str]]:
    """Citations for a field; also accept the *_quote path or the whole supplier entry."""
    for path in (f"suppliers[{i}].{name}", f"suppliers[{i}].{name}_quote", f"suppliers[{i}]"):
        if path in gmap and gmap[path][0]:
            return gmap[path]
    return [], None


def _build_evidence(
    raw: dict, name: str, cited_urls: list[str], confidence, results, supplier_domain: str
) -> Evidence:
    value = (raw.get(name) or "").strip()
    quote = (raw.get(f"{name}_quote") or "").strip()
    if name == "certifications" and value and quote:
        value = _filter_certs(value, quote)
    if not value or value.lower() == NOT_VERIFIED.lower() or not quote:
        return Evidence(NOT_VERIFIED)

    # Find which returned page contains the quote verbatim (normalized).
    nq = _norm(quote)
    matched_url = None
    for r in results:
        page_text = _norm(" ".join(r.highlights or []) + " " + (getattr(r, "text", None) or ""))
        if nq and nq in page_text:
            matched_url = r.url
            break

    url = cited_urls[0] if cited_urls else matched_url
    if not url:
        # No citation and the quote isn't in anything we retrieved: don't state it.
        return Evidence(NOT_VERIFIED)
    if name == "certifications":
        # "Mentioned on site" only when the source is the supplier's own domain.
        src = domain_of(url)
        on_own_site = src == supplier_domain or src.endswith("." + supplier_domain)
        value = f"Mentioned on site: {value}" if on_own_site else f"Mentioned on {src}: {value}"
    return Evidence(value, url, quote, confidence, quote_matched=matched_url is not None)


def search_suppliers(query: str, mode: str = "deep", exa: Optional[Exa] = None) -> SearchRun:
    run = SearchRun(query=query, mode=mode)
    query = (query or "").strip()
    if not query:
        run.error = "Please enter a supplier requirement."
        return run

    try:
        exa = exa or get_client()
        start = time.perf_counter()
        resp = exa.search(
            query,
            type=mode,
            objective=OBJECTIVE,
            system_prompt=SYSTEM_PROMPT,
            output_schema=OUTPUT_SCHEMA,
            contents={"highlights": True},
        )
        run.latency_s = round(time.perf_counter() - start, 2)
    except Exception as e:  # network, auth, 4xx/5xx from the API
        run.error = f"Exa API error: {e}"
        return run

    run.result_urls = [r.url for r in resp.results]
    if resp.search_time is not None:
        run.api_search_time_s = round(resp.search_time / 1000, 2)
    if resp.cost_dollars and resp.cost_dollars.total is not None:
        run.cost_usd = resp.cost_dollars.total
    else:
        run.cost_usd = ESTIMATED_COST_PER_REQUEST.get(mode)
        run.cost_source = "estimated"

    content = resp.output.content if resp.output else None
    raw_suppliers = content.get("suppliers", []) if isinstance(content, dict) else []
    gmap = _grounding_map(resp.output)

    seen: set[str] = set()
    for i, raw in enumerate(raw_suppliers):
        name = (raw.get("name") or "").strip()
        dom = own_site_domain(raw.get("website", ""))
        key = dom or name.lower()  # dedupe by company domain (by name if no own site)
        if not key or key in seen:
            continue
        seen.add(key)

        name_urls, _ = _cited(gmap, i, "name")
        site_source = name_urls[0] if name_urls else next(
            (u for u in run.result_urls if dom and domain_of(u) == dom), None
        )
        fields = {}
        for f in FIELDS:
            urls, conf = _cited(gmap, i, f)
            fields[f] = _build_evidence(raw, f, urls, conf, resp.results, dom or "")
        run.suppliers.append(
            Supplier(
                name=name or dom,
                website=f"https://{dom}" if dom else NOT_VERIFIED,
                website_source=site_source,
                fields=fields,
            )
        )
        if len(run.suppliers) == TOP_N:
            break

    if not run.suppliers:
        run.error = "No suppliers found for this requirement. Try broadening the region or process."
    return run
