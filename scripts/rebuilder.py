#!/usr/bin/env python3
"""
rebuilder.py
يدمج المقاطع الأصلية (parsed.json) مع الترجمات (translated.json) في ملف
SRT نهائي ثنائي اللغة، بحيث يحتفظ كل مقطع بتوقيته الزمني الأصلي تمامًا،
لكن نصه يصبح سطرين: الإنجليزي أولًا ثم العربي المترجم أسفله مباشرة.
"""

import json
import textwrap
from pathlib import Path

import srt

DATA_DIR = Path("data")
OUTPUT_DIR = Path("output")
PARSED_PATH = DATA_DIR / "parsed.json"
TRANSLATED_PATH = DATA_DIR / "translated.json"
CONFLICTS_PATH = DATA_DIR / "translation_conflicts.json"
GROQ_USED_PATH = DATA_DIR / "groq_fallback_used.json"

# معيار احترافي شائع للترجمة المرئية: حد أقصى ~42 حرفًا للسطر، وسطرين
# كحد أقصى لكل مقطع، وإلا يصعب قراءة السطر خلال مدة عرضه القصيرة.
MAX_LINE_CHARS = 42
MAX_LINES = 2
CONFLICT_MARKER = "⚠️ "
GROQ_MARKER = "🔄 "  # سطر تُرجم عبر خط الدفاع الأخير (Groq) بدل Gemini — يُنصح بمراجعته


def parse_timestamp(ts: str):
    # يعيد استخدام محلل توقيتات مكتبة srt نفسها لضمان التطابق التام
    # مع الصيغة التي أنتجها srt_parser.py
    dummy = f"1\n{ts} --> {ts}\nx\n"
    sub = next(srt.parse(dummy))
    return sub.start


def wrap_arabic_line(text: str, max_chars: int = MAX_LINE_CHARS, max_lines: int = MAX_LINES) -> str:
    """
    يقسّم نص الترجمة العربية إلى سطر أو سطرين وفق حد أقصى للأحرف، مع
    احترام حدود الكلمات (لا يقطع كلمة في المنتصف). لو النص أطول من أن
    يسعه سطرين حتى بعد التقسيم، يُدمج الفائض في السطر الثاني بدل حذفه —
    لا نفقد أي جزء من الترجمة، فقط يتجاوز السطر الثاني الحد نادرًا.
    """
    text = text.strip()
    if not text:
        return text

    wrapped = textwrap.wrap(
        text, width=max_chars, break_long_words=False, break_on_hyphens=False
    )

    if len(wrapped) <= max_lines:
        return "\n".join(wrapped)

    head = wrapped[:max_lines - 1]
    tail = " ".join(wrapped[max_lines - 1:])
    return "\n".join(head + [tail])


def main() -> None:
    parsed = json.loads(PARSED_PATH.read_text(encoding="utf-8"))
    translated = json.loads(TRANSLATED_PATH.read_text(encoding="utf-8"))
    conflicts = (
        json.loads(CONFLICTS_PATH.read_text(encoding="utf-8"))
        if CONFLICTS_PATH.exists()
        else {}
    )
    groq_used = set(
        json.loads(GROQ_USED_PATH.read_text(encoding="utf-8"))
        if GROQ_USED_PATH.exists()
        else []
    )

    segments = parsed["segments"]
    source_name = parsed["source_file"]

    subtitles = []
    missing = []

    for seg in segments:
        idx_str = str(seg["index"])
        arabic = translated.get(idx_str)
        if arabic is None:
            missing.append(seg["index"])
            arabic = "[لم تُترجم — راجع السجلات]"
        else:
            arabic = wrap_arabic_line(arabic)
            if idx_str in conflicts:
                # علامة تنبيه واضحة داخل الملف نفسه لأي سطر تُرجم بشكل
                # مختلف بين نافذتين متداخلتين، إلى جانب تفاصيله في
                # translation_conflicts.json وسجل التشغيل.
                arabic = CONFLICT_MARKER + arabic
            if idx_str in groq_used:
                # علامة لأي سطر تُرجم عبر خط الدفاع الأخير (Groq) بدل
                # Gemini بسبب استنفاد كل الحصص — يستحق مراجعة إضافية.
                arabic = GROQ_MARKER + arabic

        bilingual_content = f'{seg["content"]}\n{arabic}'

        subtitles.append(
            srt.Subtitle(
                index=seg["index"],
                start=parse_timestamp(seg["start"]),
                end=parse_timestamp(seg["end"]),
                content=bilingual_content,
            )
        )

    if missing:
        print(f"تحذير: {len(missing)} مقطعًا بلا ترجمة: {missing}")
    if conflicts:
        print(
            f"تنبيه: {len(conflicts)} سطرًا معلَّمًا بـ {CONFLICT_MARKER.strip()} "
            f"في الملف الناتج بسبب اختلاف ترجمة في نوافذ متداخلة — "
            f"التفاصيل في {CONFLICTS_PATH}."
        )
    if groq_used:
        print(
            f"تنبيه: {len(groq_used)} سطرًا معلَّمًا بـ {GROQ_MARKER.strip()} "
            f"في الملف الناتج لأنه تُرجم عبر خط الدفاع الأخير (Groq) بدل Gemini — "
            f"يُنصح بمراجعتها، التفاصيل في {GROQ_USED_PATH}."
        )

    OUTPUT_DIR.mkdir(exist_ok=True)
    stem = Path(source_name).stem
    # علامة تحذير واضحة على مستوى اسم الملف نفسه لو بقيت مقاطع بلا ترجمة،
    # حتى لا يُعتمد الملف كـ"مكتمل" بالخطأ لمجرد وجوده في output/.
    filename_prefix = "INCOMPLETE_" if missing else ""
    output_path = OUTPUT_DIR / f"{filename_prefix}{stem}.bilingual.srt"

    # utf-8-sig يكتب BOM في بداية الملف، مطلوب لبعض مشغلات الفيديو
    # القديمة وتطبيقات الجوال لعرض العربية بشكل صحيح تلقائيًا.
    output_path.write_text(srt.compose(subtitles), encoding="utf-8-sig")

    print(f"تم بناء الملف ثنائي اللغة بنجاح: {output_path}")
    print(f"إجمالي المقاطع: {len(subtitles)}")
    if missing:
        print(
            f"تنبيه: الملف يحمل بادئة INCOMPLETE_ لأن {len(missing)} مقطعًا "
            f"بقي بلا ترجمة — لا يُعتمد كملف نهائي قبل إعادة التشغيل لإكماله."
        )


if __name__ == "__main__":
    main()
