"""Analysis schema, validation and the deterministic quick-analysis fallback."""
from __future__ import annotations

from pathlib import Path

from app.evidence import evidence_pages

SKILL_PATH = Path(__file__).resolve().parent / "skills" / "saas-site-intelligence.md"


def load_skill_instructions():
    return SKILL_PATH.read_text(encoding="utf-8")[:14000]


ANALYSIS_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "product_summary": {"type": "string"}, "target_customers": {"type": "string"},
        "problem": {"type": "string"}, "value_proposition": {"type": "string"},
        "competitive_landscape": {"type": "string"},
        "workflows": {"type": "array", "items": {"type": "string"}},
        "evidence_refs": {"type": "array", "items": {"type": "string"}},
        "unknowns": {"type": "array", "items": {"type": "string"}},
    }, "required": ["product_summary", "target_customers", "problem", "value_proposition", "competitive_landscape", "workflows", "evidence_refs", "unknowns"]
}


def validate_analysis(data, allowed_refs=None):
    if not isinstance(data, dict) or set(data) != set(ANALYSIS_SCHEMA["required"]):
        raise ValueError("Analysis fields do not match the required schema.")
    if not isinstance(data, dict) or any(not isinstance(data.get(k), str) or not data[k].strip()
        for k in ("product_summary", "target_customers", "problem", "value_proposition", "competitive_landscape")):
        raise ValueError("Analysis is missing a required product claim.")
    if any(len(data[k]) > 2000 for k in ("product_summary", "target_customers", "problem", "value_proposition", "competitive_landscape")):
        raise ValueError("Analysis text exceeds the field length limit.")
    for key in ("workflows", "evidence_refs", "unknowns"):
        if not isinstance(data.get(key), list) or len(data[key]) > 12 or any(not isinstance(v, str) or len(v) > 500 for v in data[key]):
            raise ValueError(f"Analysis field {key} is invalid.")
    refs = data["evidence_refs"]
    allowed = set(allowed_refs) if allowed_refs is not None else {"homepage"}
    if not refs or any(ref not in allowed for ref in refs):
        raise ValueError("Every product claim must cite fetched page evidence.")
    return {key: data[key] for key in ANALYSIS_SCHEMA["required"]}


def quick_analysis(evidence):
    """Return a clearly provisional summary when model inference is not available yet."""
    pages = evidence_pages(evidence)
    homepage = next((page for page in pages if page.get("ref") == "homepage"), pages[0] if pages else {})
    title = str(homepage.get("title") or "").strip()[:300]
    description = str(homepage.get("description") or "").strip()[:500]
    visible = str(homepage.get("visible_text") or "").strip()
    if title or description:
        summary = "Ön bulgu: " + (title or description)
        if title and description and description.lower() not in title.lower():
            summary += " — " + description
    else:
        summary = "Sayfada ürünü tanımlamak için yeterli başlık veya açıklama bulunamadı."
    source = {"label": title or "Public homepage", "url": homepage.get("url"),
              "retrieved_at": homepage.get("retrieved_at"), "sha256": homepage.get("sha256")}
    unknown = ["Hedef müşteri sayfa kanıtından doğrulanmadı.",
               "Çözülen problem sayfa kanıtından doğrulanmadı.",
               "Ürün değeri sayfa kanıtından doğrulanmadı.",
               "Rakip araştırması yapılmadı."]
    if not visible and not description:
        unknown.insert(0, "Sayfanın görünen içeriği yetersiz; ürün analizi yapılamadı.")
    return {"analysis_stage": "preliminary", "product_summary": summary,
            "target_customers": "Sayfa kanıtından doğrulanmadı.",
            "problem": "Sayfa kanıtından doğrulanmadı.",
            "value_proposition": "Sayfa kanıtından doğrulanmadı.",
            "competitive_landscape": "Rakip araştırması yapılmadı; doğrulanmadı.",
            "workflows": [], "evidence_refs": [homepage.get("ref") or "homepage"],
            "unknowns": unknown, "sources": [source] if isinstance(source.get("url"), str) else []}
