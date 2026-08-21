#!/usr/bin/env python3
"""
srt_parser.py
يقرأ ملف SRT خام من مجلد input/ ويحوّله إلى قائمة كائنات JSON مهيكلة
(index, start, end, content) تُحفظ في data/parsed.json ليستخدمها بقية
خط الأنابيب (chunker.py ثم rebuilder.py).
"""

import json
import sys
from pathlib import Path

import srt

INPUT_DIR = Path("input")
DATA_DIR = Path("data")
PARSED_PATH = DATA_DIR / "parsed.json"


def find_input_srt() -> Path:
    """يبحث عن أول ملف .srt داخل مجلد input/."""
    candidates = sorted(INPUT_DIR.glob("*.srt"))
    if not candidates:
        sys.exit(
            "لم يتم العثور على أي ملف .srt داخل مجلد input/. "
            "ضع الملف هناك عبر GitHub mobile app ثم أعد التشغيل."
        )
    if len(candidates) > 1:
        print(
            f"تنبيه: تم العثور على {len(candidates)} ملفات SRT، "
            f"سيتم استخدام أول ملف فقط: {candidates[0].name}"
        )
    return candidates[0]


def parse_srt_file(path: Path) -> list[dict]:
    raw_text = path.read_text(encoding="utf-8-sig")
    subtitles = list(srt.parse(raw_text))

    segments = []
    for sub in subtitles:
        segments.append(
            {
                "index": sub.index,
                "start": srt.timedelta_to_srt_timestamp(sub.start),
                "end": srt.timedelta_to_srt_timestamp(sub.end),
                "content": sub.content.strip(),
            }
        )
    return segments


def main() -> None:
    DATA_DIR.mkdir(exist_ok=True)

    src_path = find_input_srt()
    print(f"جارٍ تحليل الملف: {src_path}")

    segments = parse_srt_file(src_path)
    print(f"تم استخراج {len(segments)} مقطعًا بنجاح.")

    PARSED_PATH.write_text(
        json.dumps(
            {"source_file": src_path.name, "segments": segments},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"تم الحفظ في: {PARSED_PATH}")


if __name__ == "__main__":
    main()
