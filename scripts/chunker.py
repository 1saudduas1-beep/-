#!/usr/bin/env python3
"""
chunker.py
يجمّع المقاطع المتسلسلة (الناتجة من srt_parser.py) في نوافذ سياقية
(chunks) من 20-30 مقطعًا مع هامش تداخل (overlap) بسيط بين كل نافذة
والتي تليها، حتى لا تُفقد جملة تمتد بين نهاية نافذة وبداية أخرى.
"""

import json
from pathlib import Path

DATA_DIR = Path("data")
PARSED_PATH = DATA_DIR / "parsed.json"
CHUNKS_PATH = DATA_DIR / "chunks.json"

WINDOW_SIZE = 25   # عدد المقاطع في كل نافذة
OVERLAP = 3        # عدد مقاطع التداخل بين نافذة وأخرى


def build_chunks(segments: list[dict], window_size: int, overlap: int) -> list[dict]:
    if overlap >= window_size:
        raise ValueError("overlap يجب أن يكون أصغر من window_size")

    chunks = []
    step = window_size - overlap
    i = 0
    chunk_id = 0

    while i < len(segments):
        window = segments[i : i + window_size]
        if not window:
            break

        # الأسطر "الجديدة" فقط في هذه النافذة (بدون التداخل المكرر من
        # النافذة السابقة) هي التي يجب أن تُكتب فعليًا في الملف النهائي؛
        # نُبقي معلومة ذلك هنا حتى يستخدمها rebuilder.py عند الدمج.
        new_start_index = 0 if chunk_id == 0 else overlap

        chunks.append(
            {
                "chunk_id": chunk_id,
                "segments": window,
                "new_start_index": new_start_index,
            }
        )

        chunk_id += 1
        i += step

    return chunks


def main() -> None:
    parsed = json.loads(PARSED_PATH.read_text(encoding="utf-8"))
    segments = parsed["segments"]

    chunks = build_chunks(segments, WINDOW_SIZE, OVERLAP)

    print(
        f"تم تقسيم {len(segments)} مقطعًا إلى {len(chunks)} نافذة سياقية "
        f"(حجم النافذة={WINDOW_SIZE}، تداخل={OVERLAP})."
    )

    CHUNKS_PATH.write_text(
        json.dumps({"chunks": chunks}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"تم الحفظ في: {CHUNKS_PATH}")


if __name__ == "__main__":
    main()
