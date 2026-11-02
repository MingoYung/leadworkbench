"""Explicitly synthetic, offline walkthrough; never presented as live buyers."""
from .models import FACT_FIELDS, PROMPT_VERSION


def synthetic_records():
    rows = [
        ("demo-bakery", "演示甜品店", "We run a small bakery. Looking for a supplier of 300 custom cake boxes. Delivery to Australia. Please quote samples first.", "buyer", "keep", "A"),
        ("demo-seller", "演示包装厂家", "We manufacture custom cake boxes. MOQ 1000. Contact our factory for wholesale prices.", "seller", "exclude", "C"),
        ("demo-question", "演示零售店", "Thinking about custom packaging for our shop. Does anyone have examples? No quantity or purchase date decided.", "discussion", "needs_info", "B"),
        ("demo-watch", "演示咖啡店", "Our new coffee shop opens next year. We may consider branded takeaway cups once the design is ready.", "discussion", "watch", "C"),
    ]
    return [{"source_url": f"https://{slug}.invalid/posts/synthetic-001", "company_website": f"https://{slug}.invalid/", "platform": "demo", "brand_name": name, "content": content, "is_demo": True, "is_partial": False, "metadata": {"synthetic": True, "demo_role": role, "demo_decision": decision, "demo_priority": priority, "notice": "完全合成演示；不是实际买家、实际帖子或模型分析"}} for slug, name, content, role, decision, priority in rows]


def synthetic_analysis(record):
    if record.get("is_demo") is not True or ".invalid" not in record.get("source_url", ""):
        raise ValueError("合成分析仅可用于明确标记的.invalid演示数据")
    metadata = record.get("metadata", {})
    return {"role": metadata.get("demo_role", "unknown"), "priority": metadata.get("demo_priority", "unknown"), "decision": metadata.get("demo_decision", "needs_info"), "reason": "【合成演示】展示候选筛选流程；未调用模型，不能当作真实订单。", "facts": {}, "unknown_fields": list(FACT_FIELDS), "citations": [{"field": "role", "quote": record["content"], "source_url": record["source_url"]}], "evidence_quote": record["content"], "model": "synthetic-demo (no model call)", "protocol": "demo", "prompt_version": PROMPT_VERSION, "analysis_status": "synthetic_demo", "is_synthetic": True}
