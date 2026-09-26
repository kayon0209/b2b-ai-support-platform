"""Feature list 3.6: the industry glossary that makes mixed-language search work.

The mechanism already exists - `knowledge_aliases` maps a surface form to a
canonical term, and `expand_with_aliases` appends the canonical term to the
query without touching the customer's words. What was missing is the data:
nothing told the platform that "impedance" and "阻抗" are the same subject, so a
customer writing the English term could not reach a Chinese document.

**Why two rows per concept, not one.** Expansion is directional: a row means
"when this alias appears, also search for that term". One row only bridges one
way, so a Chinese-speaking customer asking 阻抗 would still miss the English
wording (in a datasheet, a supplier page, a customer's forwarded email). Two
rows make the bridge symmetric, which is what "mixed-language" actually means
here.

**Why weights.** `expand_with_aliases` and the RRF fusion treat terms as
evidence, and not all of these are equally specific: "impedance"/阻抗 names one
subject, while "BOM" is a document type that appears in many. Weighting the
specific ones higher keeps a generic term from dragging unrelated documents
into the candidate set - the same reasoning as the business-line weights.

**Nothing here is invented.** Every entry is a term this product's domain
actually uses (PCB fabrication, SMT assembly, component sourcing, delivery),
and the canonical side is the Chinese form because the corpus is Chinese.
A glossary is data a tenant can extend; it is not a place to guess.
"""

from __future__ import annotations

# (alias, canonical term, weight). Canonical is always the Chinese form: the
# corpus is Chinese, so bridging towards it is what raises recall.
_TERMS: tuple[tuple[str, str, float], ...] = (
    # --- PCB fabrication ---
    ("impedance", "阻抗", 1.0),
    ("阻抗", "impedance", 1.0),
    ("gerber", "gerber文件", 1.0),
    ("solder mask", "阻焊", 1.0),
    ("阻焊", "solder mask", 1.0),
    ("silkscreen", "丝印", 1.0),
    ("丝印", "silkscreen", 1.0),
    ("via", "过孔", 1.0),
    ("过孔", "via", 1.0),
    ("panel", "拼板", 1.0),
    ("拼板", "panel", 1.0),
    ("copper thickness", "铜厚", 1.0),
    ("铜厚", "copper thickness", 1.0),
    ("fr4", "fr4板材", 1.0),
    ("drill", "钻孔", 1.0),
    ("钻孔", "drill", 1.0),
    ("annular ring", "环宽", 1.0),
    # --- SMT assembly ---
    ("stencil", "钢网", 1.0),
    ("钢网", "stencil", 1.0),
    ("reflow", "回流焊", 1.0),
    ("回流焊", "reflow", 1.0),
    ("wave solder", "波峰焊", 1.0),
    ("波峰焊", "wave solder", 1.0),
    ("pick and place", "贴片", 1.0),
    ("贴片", "pick and place", 1.0),
    ("solder paste", "锡膏", 1.0),
    ("锡膏", "solder paste", 1.0),
    ("aoi", "aoi检测", 1.0),
    # --- Component sourcing ---
    ("datasheet", "规格书", 1.0),
    ("规格书", "datasheet", 1.0),
    ("moq", "最小起订量", 1.0),
    ("最小起订量", "moq", 1.0),
    ("substitute", "替代料", 1.0),
    ("替代料", "substitute", 1.0),
    ("footprint", "封装", 1.0),
    ("封装", "footprint", 1.0),
    ("rohs", "rohs认证", 1.0),
    # Generic document type: lower weight, because it names a form rather than
    # a subject and would otherwise pull unrelated rows into the candidates.
    ("bom", "bom清单", 0.5),
    ("bom清单", "bom", 0.5),
    # --- Delivery and commercial ---
    ("lead time", "交期", 1.0),
    ("交期", "lead time", 1.0),
    ("eta", "预计到货", 1.0),
    ("预计到货", "eta", 1.0),
    ("purchase order", "采购订单", 1.0),
    ("采购订单", "purchase order", 1.0),
    ("rma", "退货授权", 1.0),
    ("退货授权", "rma", 1.0),
    ("expedite", "加急", 1.0),
    ("加急", "expedite", 1.0),
)


def glossary_rows() -> tuple[tuple[str, str, float], ...]:
    """The shipped glossary as (alias, term, weight), ready to insert.

    Same shape `load_aliases` returns, so a seeder can insert these directly
    and the retrieval path needs no change.
    """
    return _TERMS


def concepts() -> int:
    """How many distinct concepts the glossary bridges.

    Counted as unordered pairs rather than rows: some entries are stored in
    both directions and some are not (a narrow alias like "gerber" has no
    useful English-side target), so `len(rows) / 2` would be wrong in both
    directions depending on which entries happen to be symmetric.
    """
    return len({frozenset((alias, term)) for alias, term, _ in _TERMS})


__all__ = ["concepts", "glossary_rows"]
