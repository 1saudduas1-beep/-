#!/usr/bin/env python3
"""
translator.py
يستدعي Gemini API فعليًا لترجمة كل نافذة سياقية (chunk) إلى العربية،
مع الالتزام بقاموس المصطلحات المعتمد، وطلب رد بصيغة JSON منظّمة فقط،
والتحقق من تطابق عدد الأسطر المُرجعة مع عدد الأسطر المُرسلة، مع إعادة
محاولة تلقائية (retry بتأخير تصاعدي) عند أي خلل أو ضغط مؤقت على خوادم
Google (خطأ 503)، وفاصل زمني بسيط بين الطلبات لتفادي حدود الحصة المجانية.

يستخدم مكتبة google-genai الرسمية الجديدة (وليس google-generativeai
المتوقفة)، لأنها تتعامل بشكل صحيح مع صيغة مفاتيح API الجديدة (AQ.).

مقاوم للانقطاع: يحفظ كل نافذة مترجمة فور نجاحها في data/translated.json،
وعند إعادة التشغيل يتجاوز أي نافذة سبق ترجمتها بنجاح ويكمل من حيث توقف.
"""

import json
import os
import sys
import time
from pathlib import Path

from google import genai
from google.genai import types

from glossary_manager import (
    load_glossary,
    save_glossary,
    merge_new_terms,
    glossary_as_prompt_block,
)

DATA_DIR = Path("data")
CHUNKS_PATH = DATA_DIR / "chunks.json"
TRANSLATED_PATH = DATA_DIR / "translated.json"

MODEL_NAME = "gemini-flash-latest"
DELAY_BETWEEN_REQUESTS_SECONDS = 4
MAX_RETRIES_PER_CHUNK = 5
BACKOFF_BASE_SECONDS = 10       # التأخير يتضاعف مع كل محاولة: 10, 20, 40, 80, 160
BACKOFF_MAX_SECONDS = 120

SYSTEM_INSTRUCTIONS = """أنت مترجم طبي متخصص (medical translator). مهمتك ترجمة
سطور من محاضرة تشريح طبية باللغة الإنجليزية إلى اللغة العربية الفصحى
العلمية، مع الحفاظ الدقيق على كل المصطلحات التشريحية وضمان اتساقها.

قواعد إلزامية:
1. حافظ على الدقة العلمية لكل مصطلح تشريحي أو طبي.
2. التزم حرفيًا بأي مصطلح موجود مسبقًا في "قاموس المصطلحات المعتمد"
   أدناه؛ لا تُترجمه بصيغة مختلفة.
3. أي مصطلح طبي جديد لم يظهر في القاموس بعد، ترجمه بأفضل مقابل علمي
   عربي متعارف عليه، وأضِفه إلى new_terms في ردك.
4. رد بصيغة JSON فقط، دون أي نص إضافي قبله أو بعده، وبدون أسوار
   Markdown (```), وفق المخطط التالي بالضبط:

{
  "translations": [
    {"index": <رقم المقطع>, "arabic": "<الترجمة العربية>"},
    ...
  ],
  "new_terms": [
    {"term_en": "<المصطلح الإنجليزي>", "term_ar": "<الترجمة المعتمدة>"},
    ...
  ]
}

5. عدد عناصر translations يجب أن يساوي بالضبط عدد الأسطر المُرسلة إليك،
   بنفس أرقام index تمامًا، بما فيها الأسطر القصيرة جدًا (مثل "Right."
   أو "Okay.") — لا تدمج سطرين معًا ولا تحذف أي سطر."""


def load_translated() -> dict[str, str]:
    """يحمّل الترجمات المحفوظة مسبقًا (إن وُجدت) لدعم الاستئناف بعد انقطاع."""
    if not TRANSLATED_PATH.exists():
        return {}
    try:
        return json.loads(TRANSLATED_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_translated(all_translations: dict[str, str]) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    TRANSLATED_PATH.write_text(
        json.dumps(all_translations, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def chunk_already_done(chunk: dict, all_translations: dict[str, str]) -> bool:
    """نافذة تُعتبر منجزة إن كانت كل أسطرها موجودة مسبقًا في الترجمات."""
    return all(
        str(seg["index"]) in all_translations for seg in chunk["segments"]
    )


def build_prompt(chunk: dict, glossary: dict) -> str:
    glossary_block = glossary_as_prompt_block(glossary)
    lines_block = "\n".join(
        f'{seg["index"]}: {seg["content"]}' for seg in chunk["segments"]
    )
    return f"""قاموس المصطلحات المعتمد حتى الآن:
{glossary_block}

النافذة الحالية من الأسطر (رقم المقطع: النص الإنجليزي):
{lines_block}

ترجم كل سطر أعلاه للعربية وفق القواعد والمخطط المحددين في تعليمات النظام."""


def extract_json(raw_text: str) -> dict:
    """يزيل أسوار Markdown إن وُجدت ثم يحلّل النص كـ JSON."""
    cleaned = raw_text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
    return json.loads(cleaned.strip())


def translate_chunk(client, chunk: dict, glossary: dict) -> dict:
    expected_indices = {seg["index"] for seg in chunk["segments"]}
    prompt = build_prompt(chunk, glossary)

    last_error = None
    for attempt in range(1, MAX_RETRIES_PER_CHUNK + 1):
        try:
            response = client.models.generate_content(
                model=MODEL_NAME,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_INSTRUCTIONS,
                    response_mime_type="application/json",
                ),
            )
            parsed = extract_json(response.text)

            translations = parsed.get("translations", [])
            got_indices = {t["index"] for t in translations}

            if got_indices != expected_indices:
                missing = expected_indices - got_indices
                extra = got_indices - expected_indices
                raise ValueError(
                    f"عدم تطابق في عدد الأسطر: مفقود={missing}, زائد={extra}"
                )

            return parsed

        except Exception as exc:  # noqa: BLE001 - نريد إعادة المحاولة لأي خطأ
            last_error = exc
            if attempt < MAX_RETRIES_PER_CHUNK:
                wait = min(BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)), BACKOFF_MAX_SECONDS)
                print(
                    f"  [نافذة {chunk['chunk_id']}] محاولة {attempt} فشلت: {exc}. "
                    f"إعادة المحاولة بعد {wait} ثانية..."
                )
                time.sleep(wait)

    sys.exit(
        f"فشلت ترجمة النافذة {chunk['chunk_id']} بعد {MAX_RETRIES_PER_CHUNK} "
        f"محاولات. آخر خطأ: {last_error}\n"
        f"الترجمات المنجزة حتى الآن محفوظة في {TRANSLATED_PATH} — "
        f"إذا سويت الـ workflow تجيب هذه الملفات، أعد تشغيله وسيكمل تلقائيًا "
        f"من هذه النافذة بدل البدء من الصفر."
    )


def main() -> None:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        sys.exit(
            "متغير البيئة GEMINI_API_KEY غير موجود. تأكد من إضافته ضمن "
            "GitHub Secrets وربطه في ملف الـ workflow."
        )

    client = genai.Client(api_key=api_key)

    chunks_data = json.loads(CHUNKS_PATH.read_text(encoding="utf-8"))
    chunks = chunks_data["chunks"]

    glossary = load_glossary()
    all_translations = load_translated()

    if all_translations:
        print(
            f"تم العثور على {len(all_translations)} سطرًا مترجمًا مسبقًا "
            f"(من تشغيل سابق) — سيتم تخطي النوافذ المنجزة والاستئناف."
        )

    for chunk in chunks:
        if chunk_already_done(chunk, all_translations):
            print(f"النافذة {chunk['chunk_id'] + 1}/{len(chunks)}: منجزة مسبقًا، تخطّي.")
            continue

        print(f"جارٍ ترجمة النافذة {chunk['chunk_id'] + 1}/{len(chunks)}...")

        result = translate_chunk(client, chunk, glossary)

        for t in result.get("translations", []):
            all_translations[str(t["index"])] = t["arabic"]

        glossary = merge_new_terms(glossary, result.get("new_terms", []))

        # حفظ فوري بعد كل نافذة ناجحة، حتى لا تُفقد الترجمات المنجزة
        # إذا فشلت نافذة لاحقة أو انقطع التشغيل.
        save_glossary(glossary)
        save_translated(all_translations)

        time.sleep(DELAY_BETWEEN_REQUESTS_SECONDS)

    print(f"تمت ترجمة {len(all_translations)} سطرًا بنجاح إجمالًا.")
    print(f"القاموس النهائي يحتوي {len(glossary)} مصطلحًا معتمدًا.")
    print(f"تم الحفظ في: {TRANSLATED_PATH}")


if __name__ == "__main__":
    main()
