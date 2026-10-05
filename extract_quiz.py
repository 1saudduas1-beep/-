"""
يستخرج أسئلة اختبار NotebookLM (رابط المشاركة) إلى ملف txt (+ json) عبر Playwright.

الاستخدام:
    python extract_quiz.py <share_url> <output_base> [reveal=1|0]

يُنتج: <output_base>.txt و <output_base>.json
reveal=1: ينقر أول خيار في كل سؤال لإظهار الإجابة الصحيحة/الشرح ثم يلتقطها.
عند الفشل: debug_quiz.png و debug_quiz.txt (يتضمن الرابط النهائي وكل الإطارات frames).
متغيرات بيئة اختيارية: QUIZ_HEADED=1 (تشغيل بمتصفح غير مخفي، يُستحسن مع xvfb-run).
"""
import json
import os
import re
import sys
import time

from playwright.sync_api import sync_playwright

COUNTER_LINE = re.compile(r"^\s*(\d+)\s*/\s*(\d+)\s*$")
OPTION_LINE = re.compile(r"^\s*([A-F])\s*[.)]\s*\S")
ICON_LINE = re.compile(r"^[a-z]+(?:_[a-z]+)+$")
ICON_WORDS = {"close", "check", "cancel", "lightbulb", "done"}
CORRECT_ATTR = re.compile(r"(?<!in)correct|icon:(?:check|done)", re.I)

# يضع وسمًا data-qopt على عنصر كل خيار (A, B, C...) ويعيد نصه وخصائصه.
JS_STATE = r"""
() => {
  const optRe = /^[A-F]\s*[.)]\s*\S/;
  const cnt = t => (t.match(/(^|\n)\s*[A-F]\s*[.)]\s*\S/g) || []).length;
  document.querySelectorAll('[data-qopt]').forEach(e => e.removeAttribute('data-qopt'));
  const vis = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
  const hits = Array.from(document.body.querySelectorAll('*'))
      .filter(e => vis(e) && optRe.test((e.innerText || '').trim()));
  const deepest = hits.filter(e => !hits.some(o => o !== e && e.contains(o)));
  const opts = [];
  for (const e of deepest) {
    let cur = e;
    while (cur.parentElement && cur.parentElement !== document.body &&
           cnt(cur.parentElement.innerText || '') === 1) cur = cur.parentElement;
    if (!opts.includes(cur)) opts.push(cur);
  }
  const out = opts.map((el, i) => {
    el.setAttribute('data-qopt', String(i));
    const parts = [];
    [el, ...Array.from(el.querySelectorAll('*')).slice(0, 40)].forEach(n => {
      const c = n.getAttribute && n.getAttribute('class'); if (c) parts.push(c);
      if (n.attributes) for (const a of n.attributes)
        if (/^(aria-|data-)/.test(a.name) && a.name !== 'data-qopt' && !/^aria-(label|describedby|labelledby)$/.test(a.name))
          parts.push(a.name + '=' + a.value);
    });
    (el.innerText || '').split('\n').forEach(l => {
      l = l.trim(); if (/^[a-z]+(_[a-z]+)*$/.test(l)) parts.push('icon:' + l);
    });
    return {i, text: (el.innerText || '').trim(), attrs: parts.join(' ')};
  });
  return {text: document.body.innerText || '', options: out};
}
"""

JS_COUNTER_CHANGED = r"""
(cur) => {
  const m = (document.body.innerText || '').match(/(?:^|\n)\s*(\d+)\s*\/\s*\d+\s*(?:\n|$)/);
  return !!m && parseInt(m[1], 10) !== cur;
}
"""
JS_COUNTER_EXISTS = r"""
() => /(?:^|\n)\s*\d+\s*\/\s*\d+\s*(?:\n|$)/.test(document.body.innerText || '')
"""


COUNTER_SPLIT = re.compile(r"(?m)^[ \t]*(\d+)\s*/\s*(\d+)[ \t]*$")
JS_BODY = "document.body ? (document.body.innerText || '') : ''"
JS_QUIZ_PRESENT = r"""
() => {
  const t = document.body ? (document.body.innerText || '') : '';
  return /(?:^|\n)\s*\d+\s*\/\s*\d+\s*(?:\n|$)/.test(t);
}
"""
OVERLAY_BUTTONS = [
    r"^\s*(Accept all|I agree|Agree|Reject all|Got it|Dismiss|No thanks)\s*$",
    r"^\s*(Start|Start quiz|Take quiz|Begin|Get started|Let'?s go)\s*$",
]


def norm(text: str) -> str:
    """يوحّد العدّاد لو جاء مقسومًا على عدة أسطر (1 / / 10) إلى سطر واحد."""
    return COUNTER_SPLIT.sub(r"\1 / \2", text or "")


def clean_lines(text: str) -> list[str]:
    out = []
    for ln in (text or "").splitlines():
        s = ln.strip()
        if not s or ICON_LINE.match(s) or s.lower() in ICON_WORDS:
            continue
        out.append(s)
    return out


def parse_counter(text: str):
    for ln in (text or "").splitlines():
        m = COUNTER_LINE.match(ln)
        if m:
            return int(m.group(1)), int(m.group(2))
    return None


def parse_question(text: str) -> list[str]:
    """أسطر السؤال: بين سطر العدّاد (2 / 20) وأول خيار."""
    lines = (text or "").splitlines()
    start = next((i + 1 for i, ln in enumerate(lines) if COUNTER_LINE.match(ln)), 0)
    q = []
    for ln in lines[start:]:
        if OPTION_LINE.match(ln):
            break
        q += clean_lines(ln)
    return q


def parse_option(raw: str):
    lines = clean_lines(raw)
    if not lines:
        return "", []
    m = re.match(r"^\s*([A-F])\s*[.)]\s*(.*)$", lines[0])
    letter, first = (m.group(1), m.group(2).strip()) if m else ("?", lines[0])
    return letter, ([first] if first else []) + lines[1:]


def read_state(page):
    st = page.evaluate(JS_STATE)
    st["text"] = norm(st.get("text", ""))
    return st


def body_text(page) -> str:
    return norm(page.evaluate(JS_BODY))


def capture_question(page, reveal: bool) -> dict:
    s1 = read_state(page)
    cnt = parse_counter(s1["text"])
    q = {"number": cnt[0] if cnt else None, "total": cnt[1] if cnt else None,
         "question": parse_question(s1["text"]), "options": [], "correct": [], "feedback": []}
    for o in s1["options"]:
        letter, lines = parse_option(o["text"])
        q["options"].append({"letter": letter, "lines": lines})
    if not q["options"]:
        q["raw"] = clean_lines(s1["text"])
        return q
    if reveal:
        try:
            page.locator('[data-qopt="0"]').first.click(timeout=5000)
            time.sleep(1.0)
            s2 = read_state(page)
            before = set(clean_lines(s1["text"]))
            q["feedback"] = [l for l in clean_lines(s2["text"]) if l not in before]
            for o2 in s2["options"]:
                if o2["i"] < len(q["options"]) and CORRECT_ATTR.search(o2["attrs"] or ""):
                    q["correct"].append(q["options"][o2["i"]]["letter"])
        except Exception as e:
            q["feedback"] = [f"(تعذّر كشف الإجابة: {type(e).__name__})"]
    return q


def click_button(page, pattern: str, timeout=5000) -> bool:
    btn = page.get_by_role("button", name=re.compile(pattern, re.I))
    if btn.count() == 0:
        btn = page.get_by_text(re.compile(pattern, re.I))
    if btn.count() == 0:
        return False
    btn.first.click(timeout=timeout)
    return True


def render_txt(questions: list[dict]) -> str:
    out = []
    for q in questions:
        out.append(f"[{q.get('number')}/{q.get('total')}]")
        out += q.get("question", [])
        for o in q.get("options", []):
            ls = o["lines"] or [""]
            out.append(f"{o['letter']}. {ls[0]}")
            out += [f"    {x}" for x in ls[1:]]
        if q.get("correct"):
            out.append("الإجابة الصحيحة: " + ", ".join(q["correct"]))
        if q.get("feedback"):
            out.append("ملاحظات/شرح:")
            out += [f"    {x}" for x in q["feedback"]]
        if q.get("raw"):
            out.append("(لم تُحدَّد الخيارات - النص الخام:)")
            out += [f"    {x}" for x in q["raw"]]
        out.append("")
        out.append("-" * 40)
        out.append("")
    return "\n".join(out)


def dismiss_overlays(page):
    """يغلق نوافذ الموافقة ويضغط زر البدء إن وُجد، في كل الإطارات."""
    for fr in list(page.frames):
        for pat in OVERLAY_BUTTONS:
            try:
                btn = fr.get_by_role("button", name=re.compile(pat, re.I))
                if btn.count() > 0 and btn.first.is_visible():
                    btn.first.click(timeout=2000)
                    time.sleep(1.0)
            except Exception:
                pass


def find_quiz_frame(page):
    """يبحث عن الإطار (الصفحة الرئيسية أو iframe) الذي يحوي عدّاد الأسئلة."""
    for fr in list(page.frames):
        try:
            if fr.evaluate(JS_QUIZ_PRESENT):
                return fr
        except Exception:
            continue
    return None


def wait_for_quiz(page, timeout_s=120):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if "accounts.google.com" in page.url:
            return None
        dismiss_overlays(page)
        fr = find_quiz_frame(page)
        if fr:
            return fr
        time.sleep(2)
    return None


def debug_dump(page, why: str):
    print(f"::error::{why}")
    try:
        page.screenshot(path="debug_quiz.png", full_page=True)
    except Exception:
        pass
    try:
        with open("debug_quiz.txt", "w", encoding="utf-8") as f:
            f.write(f"URL: {page.url}\nTITLE: {page.title()}\n")
            for i, fr in enumerate(page.frames):
                f.write(f"\n===== frame {i}: {fr.url} =====\n")
                try:
                    f.write(fr.evaluate(JS_BODY))
                except Exception as e:
                    f.write(f"(تعذّرت القراءة: {type(e).__name__})")
    except Exception:
        pass


def main():
    if len(sys.argv) not in (3, 4):
        print("Usage: python extract_quiz.py <share_url> <output_base> [reveal=1|0]")
        sys.exit(1)
    url, base = sys.argv[1], sys.argv[2]
    reveal = (sys.argv[3] if len(sys.argv) == 4 else "1") not in ("0", "false", "False")

    headed = os.environ.get("QUIZ_HEADED") == "1"
    ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=not headed,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        )
        opts = {"viewport": {"width": 1100, "height": 1300}, "locale": "en-US",
                "timezone_id": "America/New_York"}
        if not headed:
            opts["user_agent"] = ua
        ctx = browser.new_context(**opts)
        ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
        page = ctx.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=90000)
        except Exception as e:
            print(f"::warning::تحذير أثناء التحميل: {type(e).__name__}")

        fr = wait_for_quiz(page, 120)
        if fr is None:
            if "accounts.google.com" in page.url:
                debug_dump(page, "تمت إعادة التوجيه إلى تسجيل دخول Google؛ الرابط غير متاح للزائر المجهول من هذا الخادم.")
            else:
                debug_dump(page, "لم تظهر صفحة الاختبار (عدّاد الأسئلة). راجع debug_quiz.png و debug_quiz.txt في الـ Artifact.")
            sys.exit(1)
        time.sleep(1.5)

        # ابدأ من السؤال الأول
        for _ in range(60):
            cnt = parse_counter(body_text(fr))
            if not cnt or cnt[0] <= 1:
                break
            if not click_button(fr, r"^\s*Previous\s*$"):
                break
            try:
                fr.wait_for_function(JS_COUNTER_CHANGED, arg=cnt[0], timeout=8000)
            except Exception:
                break

        questions, seen = [], set()
        while True:
            q = capture_question(fr, reveal)
            n, total = q.get("number"), q.get("total")
            if n in seen:
                break
            seen.add(n)
            questions.append(q)
            print(f"  التقط السؤال {n}/{total}: {' '.join(q['question'])[:70]}")
            if not n or not total or n >= total:
                break
            try:
                if not click_button(fr, r"^\s*Next\s*$"):
                    print("::warning::لم يُعثر على زر Next؛ توقّف عند هذا السؤال.")
                    break
                fr.wait_for_function(JS_COUNTER_CHANGED, arg=n, timeout=10000)
                time.sleep(0.6)
            except Exception:
                print("::warning::لم ينتقل الاختبار للسؤال التالي؛ تم الاكتفاء بما التُقط.")
                break

        if not questions or not any(q.get("options") for q in questions):
            debug_dump(page, "لم تُستخرج أي أسئلة بخيارات.")
            sys.exit(1)
        browser.close()

    total = questions[0].get("total")
    if total and len(questions) < total:
        print(f"::warning::التُقط {len(questions)} من {total} سؤالًا فقط.")
    with open(f"{base}.txt", "w", encoding="utf-8") as f:
        f.write(render_txt(questions))
    with open(f"{base}.json", "w", encoding="utf-8") as f:
        json.dump(questions, f, ensure_ascii=False, indent=2)
    print(f"Done. {len(questions)} questions -> {base}.txt / {base}.json")


if __name__ == "__main__":
    main()
