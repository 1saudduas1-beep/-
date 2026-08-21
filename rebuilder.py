#!/usr/bin/env python3
"""
rebuilder.py
يدمج المقاطع الأصلية (parsed.json) مع الترجمات (translated.json) في ملف
SRT نهائي ثنائي اللغة، بحيث يحتفظ كل مقطع بتوقيته الزمني الأصلي تمامًا،
لكن نصه يصبح سطرين: الإنجليزي أولًا ثم العربي المترجم أسفله مباشرة.
"""

import json
from pathlib import Path

import srt

DATA_DIR = Path("data")
OUTPUT_DIR = Path("output")
PARSED_PATH = DATA_DIR / "parsed.json"
TRANSLATED_PATH = DATA_DIR / "translated.json"


def parse_timestamp(ts: str):
    # يعيد استخدام محلل توقيتات مكتبة srt نفسها لضمان التطابق التام
    # مع الصيغة التي أنتجها srt_parser.py
    dummy = f"1\n{ts} --> {ts}\nx\n"
    sub = next(srt.parse(dummy))
    return sub.start


def main() -> None:
    parsed = json.loads(PARSED_PATH.read_text(encoding="utf-8"))
    translated = json.loads(TRANSLATED_PATH.read_text(encoding="utf-8"))

    segments = parsed["segments"]
    source_name = parsed["source_file"]

    subtitles = []
    missing = []

    for seg in segments:
        arabic = translated.get(str(seg["index"]))
        if arabic is None:
            missing.append(seg["index"])
            arabic = "[لم تُترجم — راجع السجلات]"

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

    OUTPUT_DIR.mkdir(exist_ok=True)
    stem = Path(source_name).stem
    output_path = OUTPUT_DIR / f"{stem}.bilingual.srt"

    output_path.write_text(srt.compose(subtitles), encoding="utf-8")

    print(f"تم بناء الملف ثنائي اللغة بنجاح: {output_path}")
    print(f"إجمالي المقاطع: {len(subtitles)}")


if __name__ == "__main__":
    main()
