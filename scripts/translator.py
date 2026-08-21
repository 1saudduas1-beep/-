#!/usr/bin/env python3
"""
translator.py
يستدعي Gemini API فعليًا لترجمة كل نافذة سياقية (chunk) إلى العربية،
مع الالتزام بقاموس المصطلحات المعتمد، وطلب رد بصيغة JSON منظّمة فقط،
والتحقق من تطابق عدد الأسطر المُرجعة مع عدد الأسطر المُرسلة.

استراتيجية الصمود أمام حدود الحصة المجانية (مبنية على أن حصة Gemini
اليومية مستقلة لكل تركيبة "مشروع × موديل" على حدة):

1) Model cascade تلقائي من الأعلى جودة للأدنى: يبدأ بأحدث موديل (أعلى
   جودة، حصة يومية ضيقة عادة)، وإذا استُنفدت حصته (429) لكل المفاتيح
   المتاحة، ينزل تلقائيًا للموديل التالي الأقل جودة لكن الأسخى حصة.
2) تدوير مفاتيح API متعددة (GEMINI_API_KEYS): كل مفتاح = مشروع Google
   منفصل بحصة يومية مستقلة تمامًا. لكل درجة من الـ cascade نجرب كل
   المفاتيح المتاحة قبل النزول لدرجة أضعف جودة.
3) خط دفاع أخير: لو استُنفدت كل تركيبات (مفتاح × موديل) على Gemini في
   نفس اليوم، ونُقل مفتاح GROQ_API_KEY، نكمل الترجمة عبر Groq (نموذج
   مفتوح بديل) بدل التوقف الكامل، مع تعليم واضح للأسطر التي تُرجمت
   بهذا الخط الاحتياطي لمراجعتها لاحقًا (جودتها قد تكون أقل من Gemini).

مقاوم للانقطاع: يحفظ كل نافذة مترجمة فور نجاحها في data/translated.json،
وعند إعادة التشغيل يتجاوز أي نافذة سبق ترجمتها بنجاح ويكمل من حيث توقف.
كذلك يحفظ التركيبات (مفتاح × موديل) المستنفدة اليوم في
data/exhausted_quota.json حتى لا يُعاد تجربتها من الصفر بعد كل إعادة
تشغيل لنفس اليوم.
"""

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
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
CONFLICTS_PATH = DATA_DIR / "translation_conflicts.json"
EXHAUSTED_PATH = DATA_DIR / "exhausted_quota.json"
GROQ_USED_PATH = DATA_DIR / "groq_fallback_used.json"

# ترتيب الـ cascade من الأعلى جودة (وأضيق حصة عادة) للأسخى حصة (وأقل
# جودة نسبيًا). "gemini-flash-latest" اسم متحرك يتحدث تلقائيًا لأحدث
# موديل عند Google، فهو دومًا أعلى درجة بالـ cascade مهما تغيّر مستقبلًا.
# البقية أسماء مثبّتة صراحة (غير متحركة) كشبكة أمان بحصص أسخى.
MODEL_CASCADE = [
    "gemini-flash-latest",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-2.0-flash-lite",
]

MODEL_NAME = MODEL_CASCADE[0]  # يبقى معرَّفًا للتوافق مع أي استخدام خارجي سابق

DELAY_BETWEEN_REQUESTS_SECONDS = 4
MAX_RETRIES_PER_COMBO = 3       # محاولات على نفس تركيبة (مفتاح، موديل) لأخطاء 503/عامة فقط
BACKOFF_BASE_SECONDS = 10
BACKOFF_MAX_SECONDS = 60        # أقل من السابق لأن لدينا الآن بدائل كثيرة ننتقل إليها بسرعة

GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
GROQ_MODEL = "llama-3.3-70b-versatile"
GROQ_ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MARKER = "🔄 "  # علامة داخل الـ SRT النهائي لأي سطر تُرجم عبر خط الدفاع الأخير (Groq) بدل Gemini

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


# ------------------------------------------------------------------
# تحميل/حفظ ملفات البيانات
# ------------------------------------------------------------------

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


def load_conflicts() -> dict[str, dict]:
    if not CONFLICTS_PATH.exists():
        return {}
    try:
        return json.loads(CONFLICTS_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_conflicts(conflicts: dict[str, dict]) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    CONFLICTS_PATH.write_text(
        json.dumps(conflicts, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _today_str() -> str:
    # تقريب بسيط ليوم إعادة تعيين الحصة (يوميًا حسب توقيت المحيط الهادئ
    # فعليًا عند Google)؛ التاريخ العام هنا كافٍ عمليًا: أسوأ حالة أن
    # exhausted_quota.json يُعاد تصفيره ساعات قليلة قبل/بعد إعادة الضبط
    # الفعلية، وهو خطأ غير مكلف (يعني محاولة إضافية أو تخطي غير ضروري).
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def load_exhausted() -> set[str]:
    """يحمّل تركيبات (مفتاح، موديل) المستنفدة اليوم فقط؛ أي سجل من يوم
    سابق يُعتبر منتهي الصلاحية تلقائيًا (الحصة اليومية تجدّدت)."""
    if not EXHAUSTED_PATH.exists():
        return set()
    try:
        data = json.loads(EXHAUSTED_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return set()
    if data.get("date") != _today_str():
        return set()
    return set(data.get("combos", []))


def save_exhausted(combos: set[str]) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    EXHAUSTED_PATH.write_text(
        json.dumps({"date": _today_str(), "combos": sorted(combos)}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_groq_used() -> set[str]:
    if not GROQ_USED_PATH.exists():
        return set()
    try:
        return set(json.loads(GROQ_USED_PATH.read_text(encoding="utf-8")))
    except json.JSONDecodeError:
        return set()


def save_groq_used(indices: set[str]) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    GROQ_USED_PATH.write_text(
        json.dumps(sorted(indices, key=lambda x: int(x)), ensure_ascii=False, indent=2),
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


def classify_error(exc: Exception) -> str:
    """يصنّف الخطأ إلى quota (429) أو transient (503 وما شابهه) أو other."""
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    text = str(exc)

    if code == 429 or "429" in text or "RESOURCE_EXHAUSTED" in text or "quota" in text.lower():
        return "quota"
    if code == 503 or "503" in text or "UNAVAILABLE" in text or "overloaded" in text.lower():
        return "transient"
    return "other"


def validate_translation_shape(parsed: dict, expected_indices: set[int]) -> None:
    translations = parsed.get("translations", [])
    got_indices = {t["index"] for t in translations}
    if got_indices != expected_indices:
        missing = expected_indices - got_indices
        extra = got_indices - expected_indices
        raise ValueError(f"عدم تطابق في عدد الأسطر: مفقود={missing}, زائد={extra}")


# ------------------------------------------------------------------
# مسار Gemini: تجربة تركيبة (مفتاح، موديل) واحدة
# ------------------------------------------------------------------

def try_gemini_combo(client, model_name: str, chunk: dict, prompt: str, expected_indices: set[int]):
    """
    يحاول تركيبة (مفتاح، موديل) واحدة، مع إعادة محاولة محدودة لأخطاء
    503/عامة فقط. يرجّع (parsed_json, None) عند النجاح، أو
    (None, "quota") لو الحصة مستنفدة لهذه التركيبة (لا فائدة من إعادة
    محاولتها اليوم)، أو يرفع الاستثناء الأخير لو فشلت كل المحاولات
    لأسباب غير الحصة (503/عامة) كي يقرر المستدعي الانتقال لتركيبة أخرى.
    """
    last_error = None
    for attempt in range(1, MAX_RETRIES_PER_COMBO + 1):
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_INSTRUCTIONS,
                    response_mime_type="application/json",
                ),
            )
            parsed = extract_json(response.text)
            validate_translation_shape(parsed, expected_indices)
            return parsed, None

        except Exception as exc:  # noqa: BLE001
            last_error = exc
            kind = classify_error(exc)

            if kind == "quota":
                # لا فائدة من إعادة المحاولة على نفس التركيبة اليوم؛
                # نُبلغ المستدعي فورًا كي ينتقل لتركيبة أخرى بلا انتظار.
                return None, "quota"

            if attempt < MAX_RETRIES_PER_COMBO:
                wait = min(BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)), BACKOFF_MAX_SECONDS)
                print(
                    f"    [{model_name}] محاولة {attempt} فشلت ({kind}): {exc}. "
                    f"إعادة المحاولة بعد {wait} ثانية..."
                )
                time.sleep(wait)

    raise last_error


def translate_chunk_gemini(clients: list, chunk: dict, glossary: dict, exhausted: set[str]):
    """
    يجرب كل تركيبة (موديل، مفتاح) وفق أولوية الـ cascade (الجودة أولًا:
    لكل موديل نجرب كل المفاتيح قبل النزول للموديل الأقل جودة)، متخطّيًا
    أي تركيبة معروف مسبقًا أنها مستنفدة اليوم. يُحدّث exhausted في مكانه
    عند اكتشاف تركيبة جديدة مستنفدة. يرجّع dict الترجمة عند النجاح، أو
    None لو استُنفدت كل التركيبات المتاحة (وقتها يلجأ المستدعي لـ Groq).
    """
    expected_indices = {seg["index"] for seg in chunk["segments"]}
    prompt = build_prompt(chunk, glossary)

    for model_name in MODEL_CASCADE:
        for key_idx, client in enumerate(clients):
            combo = f"{key_idx}:{model_name}"
            if combo in exhausted:
                continue

            print(f"    تجربة: مفتاح #{key_idx + 1} × {model_name}")
            try:
                parsed, failure_kind = try_gemini_combo(
                    client, model_name, chunk, prompt, expected_indices
                )
            except Exception as exc:  # noqa: BLE001 - فشل نهائي على هذه التركيبة بعد كل محاولاتها
                print(f"    [{model_name} / مفتاح #{key_idx + 1}] فشل نهائي بعد إعادة المحاولات: {exc}")
                continue  # جرّب التركيبة التالية بدل التوقف الكامل

            if failure_kind == "quota":
                print(f"    [{model_name} / مفتاح #{key_idx + 1}] حصة مستنفدة اليوم — تخطّي لبقية التشغيل.")
                exhausted.add(combo)
                save_exhausted(exhausted)
                continue

            return parsed  # نجاح

    return None  # كل التركيبات استُنفدت أو فشلت


# ------------------------------------------------------------------
# مسار الاحتياط: Groq (خط دفاع أخير فقط عند استنفاد كل تركيبات Gemini)
# ------------------------------------------------------------------

def translate_chunk_groq(chunk: dict, glossary: dict):
    if not GROQ_API_KEY:
        return None

    expected_indices = {seg["index"] for seg in chunk["segments"]}
    prompt = build_prompt(chunk, glossary)

    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": GROQ_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_INSTRUCTIONS},
            {"role": "user", "content": prompt},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.3,
    }

    last_error = None
    for attempt in range(1, MAX_RETRIES_PER_COMBO + 1):
        try:
            resp = requests.post(GROQ_ENDPOINT, headers=headers, json=payload, timeout=90)
            if resp.status_code == 400 and "response_format" in resp.text:
                # بعض موديلات Groq قد لا تدعم response_format بعد؛ نعيد
                # المحاولة بدونه ونعتمد على extract_json لتنظيف الرد.
                payload.pop("response_format", None)
                resp = requests.post(GROQ_ENDPOINT, headers=headers, json=payload, timeout=90)

            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
            parsed = extract_json(content)
            validate_translation_shape(parsed, expected_indices)
            return parsed

        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt < MAX_RETRIES_PER_COMBO:
                wait = min(BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)), BACKOFF_MAX_SECONDS)
                print(f"    [Groq احتياطي] محاولة {attempt} فشلت: {exc}. إعادة المحاولة بعد {wait} ثانية...")
                time.sleep(wait)

    print(f"    [Groq احتياطي] فشل نهائي بعد كل المحاولات: {last_error}")
    return None


# ------------------------------------------------------------------
# التنسيق العام لترجمة نافذة واحدة عبر كل المسارات
# ------------------------------------------------------------------

def translate_chunk(clients: list, chunk: dict, glossary: dict, exhausted: set[str]) -> tuple[dict, bool]:
    """يرجّع (parsed_json, used_groq_fallback). يوقف التشغيل بالكامل فقط
    لو استُنفدت كل تركيبات Gemini ولم ينجح Groq أيضًا (أو غير مُفعّل)."""
    result = translate_chunk_gemini(clients, chunk, glossary, exhausted)
    if result is not None:
        return result, False

    print(f"  [نافذة {chunk['chunk_id']}] كل تركيبات Gemini (مفاتيح × موديلات) مستنفدة أو فاشلة.")

    if GROQ_API_KEY:
        print(f"  [نافذة {chunk['chunk_id']}] الانتقال لخط الدفاع الأخير (Groq)...")
        groq_result = translate_chunk_groq(chunk, glossary)
        if groq_result is not None:
            return groq_result, True

    sys.exit(
        f"فشلت ترجمة النافذة {chunk['chunk_id']}: كل تركيبات Gemini المتاحة "
        f"(مفاتيح × موديلات) مستنفدة الحصة اليومية"
        + (" وفشل خط الدفاع الأخير (Groq) أيضًا." if GROQ_API_KEY else "، ولا يوجد GROQ_API_KEY مُفعّل كخط دفاع أخير.")
        + f"\nالترجمات المنجزة حتى الآن محفوظة في {TRANSLATED_PATH} — "
        f"جرّب لاحقًا (غدًا مثلًا بعد تجدد حصص Gemini) وسيكمل تلقائيًا من هذه النافذة."
    )


# ------------------------------------------------------------------
# main
# ------------------------------------------------------------------

def build_clients() -> list:
    """يبني قائمة عملاء Gemini، مفتاح واحد لكل عنصر، من GEMINI_API_KEYS
    (مفصولة بفواصل) أو GEMINI_API_KEY (مفتاح واحد) كخيار متوافق مع
    الإعداد القديم."""
    keys_raw = os.environ.get("GEMINI_API_KEYS") or os.environ.get("GEMINI_API_KEY")
    if not keys_raw:
        sys.exit(
            "لا يوجد أي من GEMINI_API_KEYS أو GEMINI_API_KEY في متغيرات البيئة. "
            "تأكد من إضافة واحد منهما ضمن GitHub Secrets وربطه في ملف الـ workflow."
        )
    keys = [k.strip() for k in keys_raw.split(",") if k.strip()]
    if not keys:
        sys.exit("قائمة مفاتيح Gemini فارغة بعد التحليل — تحقق من قيمة GEMINI_API_KEYS.")

    return [genai.Client(api_key=k) for k in keys]


def main() -> None:
    clients = build_clients()
    print(f"عدد مفاتيح Gemini المتاحة: {len(clients)}")
    print(f"ترتيب الـ cascade (من الأعلى جودة): {' → '.join(MODEL_CASCADE)}")
    if GROQ_API_KEY:
        print(f"خط دفاع أخير (Groq) مُفعّل: {GROQ_MODEL}")
    else:
        print("خط دفاع Groq غير مُفعّل (لا يوجد GROQ_API_KEY).")

    chunks_data = json.loads(CHUNKS_PATH.read_text(encoding="utf-8"))
    chunks = chunks_data["chunks"]

    glossary = load_glossary()
    all_translations = load_translated()
    conflicts = load_conflicts()
    exhausted = load_exhausted()
    groq_used = load_groq_used()

    if all_translations:
        print(
            f"تم العثور على {len(all_translations)} سطرًا مترجمًا مسبقًا "
            f"(من تشغيل سابق) — سيتم تخطي النوافذ المنجزة والاستئناف."
        )
    if exhausted:
        print(f"تركيبات مستنفدة اليوم (من تشغيل سابق نفس اليوم): {sorted(exhausted)}")

    for chunk in chunks:
        if chunk_already_done(chunk, all_translations):
            print(f"النافذة {chunk['chunk_id'] + 1}/{len(chunks)}: منجزة مسبقًا، تخطّي.")
            continue

        print(f"جارٍ ترجمة النافذة {chunk['chunk_id'] + 1}/{len(chunks)}...")

        result, used_groq = translate_chunk(clients, chunk, glossary, exhausted)

        for t in result.get("translations", []):
            idx_str = str(t["index"])
            new_arabic = t["arabic"]

            if idx_str in all_translations and all_translations[idx_str] != new_arabic:
                conflicts[idx_str] = {
                    "previous": all_translations[idx_str],
                    "latest": new_arabic,
                }

            all_translations[idx_str] = new_arabic
            if used_groq:
                groq_used.add(idx_str)

        glossary = merge_new_terms(glossary, result.get("new_terms", []))

        save_glossary(glossary)
        save_translated(all_translations)
        if conflicts:
            save_conflicts(conflicts)
        if groq_used:
            save_groq_used(groq_used)

        time.sleep(DELAY_BETWEEN_REQUESTS_SECONDS)

    print(f"تمت ترجمة {len(all_translations)} سطرًا بنجاح إجمالًا.")
    print(f"القاموس النهائي يحتوي {len(glossary)} مصطلحًا معتمدًا.")
    print(f"تم الحفظ في: {TRANSLATED_PATH}")
    if conflicts:
        print(
            f"تنبيه: {len(conflicts)} سطرًا من أسطر التداخل (overlap) "
            f"تُرجمت بشكل مختلف بين نافذتين — التفاصيل في {CONFLICTS_PATH}."
        )
    if groq_used:
        print(
            f"تنبيه: {len(groq_used)} سطرًا تُرجم عبر خط الدفاع الأخير (Groq) بدل "
            f"Gemini بسبب استنفاد كل الحصص — التفاصيل في {GROQ_USED_PATH}، "
            f"ومُعلَّمة بـ {GROQ_MARKER.strip()} في ملف الـ SRT النهائي، يُنصح بمراجعتها."
        )


if __name__ == "__main__":
    main()
