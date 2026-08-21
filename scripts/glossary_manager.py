#!/usr/bin/env python3
"""
glossary_manager.py
يدير قاموس المصطلحات الطبية المتّسق (glossary.json). يبدأ فارغًا، وبعد
ترجمة كل نافذة يُضاف إليه أي مصطلح طبي جديد مع ترجمته العربية المعتمدة،
ويُرفق القاموس كاملًا ضمن التعليمات (prompt) المرسلة لكل نافذة لاحقة
حتى يلتزم النموذج بنفس الترجمة لكل مصطلح متكرر.

يمكن استيراد هذا الملف من translator.py، أو تشغيله مباشرة لعرض محتوى
القاموس الحالي.
"""

import json
from pathlib import Path

DATA_DIR = Path("data")
GLOSSARY_PATH = DATA_DIR / "glossary.json"


def load_glossary() -> dict[str, str]:
    """يحمّل القاموس الحالي، أو يبدأ قاموسًا فارغًا إن لم يكن موجودًا بعد."""
    if not GLOSSARY_PATH.exists():
        return {}
    return json.loads(GLOSSARY_PATH.read_text(encoding="utf-8"))


def save_glossary(glossary: dict[str, str]) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    GLOSSARY_PATH.write_text(
        json.dumps(glossary, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def merge_new_terms(glossary: dict[str, str], new_terms: list[dict]) -> dict[str, str]:
    """
    يدمج مصطلحات جديدة قادمة من رد النموذج بصيغة:
    [{"term_en": "trapezius", "term_ar": "العضلة شبه المنحرفة"}, ...]
    المصطلح الأول الذي يُعتمد لكل كلمة إنجليزية يبقى ثابتًا؛ لا يُستبدل
    لاحقًا، حفاظًا على الاتساق عبر الملف كاملًا.
    """
    for item in new_terms:
        term_en = item.get("term_en", "").strip()
        term_ar = item.get("term_ar", "").strip()
        if not term_en or not term_ar:
            continue
        key = term_en.lower()
        if key not in glossary:
            glossary[key] = term_ar
    return glossary


def glossary_as_prompt_block(glossary: dict[str, str]) -> str:
    """يحوّل القاموس الحالي إلى نص جاهز للحقن داخل تعليمات الترجمة."""
    if not glossary:
        return "(لا توجد مصطلحات معتمدة بعد، هذه أول نافذة تُترجم)"
    lines = [f"- {en} → {ar}" for en, ar in sorted(glossary.items())]
    return "\n".join(lines)


def main() -> None:
    glossary = load_glossary()
    print(f"عدد المصطلحات المعتمدة حاليًا: {len(glossary)}")
    print(glossary_as_prompt_block(glossary))


if __name__ == "__main__":
    main()
