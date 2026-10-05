"""
يستخرج أسئلة اختبار NotebookLM (رابط المشاركة) عبر Playwright إلى:
    <base>.json  مخطط منظَّم (meta + questions) مع تحقق جودة لكل سؤال
    <base>.txt   نص مرتب يحاكي ترتيب الصفحة

الاستخدام: python extract_quiz.py <share_url> <base> [reveal=1|0]
متغيرات اختيارية: QUIZ_HEADED=1 ، QUIZ_HINTS=1|0 ، QUIZ_DIGITS=keep|latin (تحويل الأرقام الهندية ٠-٩ إلى 0-9)
"""
import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timezone

from playwright.sync_api import sync_playwright

COUNTER_LINE = re.compile(r"^\s*(\d+)\s*/\s*(\d+)\s*$")
COUNTER_SPLIT = re.compile(r"(?m)^[ \t]*(\d+)\s*/\s*(\d+)[ \t]*$")
OPT_MARK = "@@OPT@@"
ICON_LINE = re.compile(r"^[a-z]+(?:_[a-z]+)+$")
ICON_WORDS = {"close", "check", "cancel", "lightbulb", "done"}
CORRECT_ATTR = re.compile(r"(?<!in)correct|icon:(?:check|done)", re.I)
VERDICT_OK = re.compile(r"^(right answer|correct( answer)?|الإجابة الصحيحة|إجابة صحيحة)\W*$", re.I)
VERDICT_BAD = re.compile(r"^(not quite|incorrect|wrong( answer)?|your answer is incorrect|"
                         r"إجابتك غير صحيحة|إجابة غير صحيحة|غير صحيحة)\W*$", re.I)
AR_CHAR = re.compile(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]")
INVISIBLE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff\u00ad]")
AR_PRES = re.compile(r"[\uFB50-\uFDFF\uFE70-\uFEFF]")

BTN_NEXT = r"^\s*(Next|التالي)\s*$"
BTN_PREV = r"^\s*(Previous|السابق)\s*$"
BTN_HINT = r"^\s*(Hint|تلميح)\s*$"
OVERLAY_BUTTONS = [
    r"^\s*(Accept all|I agree|Agree|Reject all|Got it|Dismiss|No thanks|قبول الكل|موافق)\s*$",
    r"^\s*(Start|Start quiz|Take quiz|Begin|Get started|Let'?s go|ابدأ|بدء)\s*$",
]

# ───────────────────────── JS (داخل الصفحة) ─────────────────────────
# يسلسل DOM بدل innerText: يدمج العناصر السطرية (inline) في سطر واحد، يحوّل KaTeX إلى LaTeX،
# يستبدل sup/sub، ويتجاهل الأيقونات والعناصر المخفية/قارئ الشاشة (مصدر تسرب نص "Not quite..." القديم).
JS_STATE = r"""
() => {
  const BIDI = /[\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]/g;
  const optRe = /^[A-F]\s*[.)]\s*\S/;
  const cntRe = /(^|\n)\s*\d+\s*\/\s*\d+\s*(\n|$)/;
  const cnt = t => (t.match(/(^|\n)\s*[A-F]\s*[.)]\s*\S/g) || []).length;
  const ICON = /(^|\s)(material-icons|material-symbols|mat-icon|google-symbols|material-symbols-outlined)(\s|$)/;
  const HID = /(visually-hidden|live-announcer|sr-only|screen-reader)/i;
  const SKIP = new Set(['SCRIPT','STYLE','NOSCRIPT','SVG','MAT-ICON','TEMPLATE']);
  function ser(n, mark) {
    if (n.nodeType === 3) return (n.nodeValue || '').replace(/\s+/g, ' ');
    if (n.nodeType !== 1) return '';
    const tag = n.tagName.toUpperCase();
    if (SKIP.has(tag)) return '';
    const cls = n.getAttribute('class') || '';
    if (ICON.test(cls) || HID.test(cls)) return '';
    const cs = getComputedStyle(n);
    if (cs.display === 'none' || cs.visibility === 'hidden') return '';
    if (cs.display !== 'contents') {
      const r = n.getBoundingClientRect();
      if (r.width <= 1 && r.height <= 1) return '';
    }
    if (mark && n.hasAttribute('data-qopt')) return '\n@@OPT@@\n';
    if (n.classList.contains('katex')) {
      const a = n.querySelector('annotation[encoding="application/x-tex"]');
      if (a) return '\u0001' + a.textContent + '\u0002';
    }
    if (tag === 'BR') return '\n';
    let s = '';
    for (const c of n.childNodes) s += ser(c, mark);
    if (tag === 'SUP') s = '^{' + s + '}';
    else if (tag === 'SUB') s = '_{' + s + '}';
    const inl = (cs.display === 'inline' || cs.display === 'contents');
    if (!inl && n.querySelector('.katex') && s.replace(/\u0001[^\u0002]*\u0002/g, '').trim() === '') return s;
    return inl ? s : '\n' + s + '\n';
  }
  document.querySelectorAll('[data-qopt]').forEach(e => e.removeAttribute('data-qopt'));
  const vis = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
  const hits = Array.from(document.body.querySelectorAll('*'))
      .filter(e => vis(e) && optRe.test(((e.innerText || '').replace(BIDI, '')).trim()));
  const deepest = hits.filter(e => !hits.some(o => o !== e && e.contains(o)));
  const opts = [];
  for (const e of deepest) {
    let cur = e;
    while (cur.parentElement && cur.parentElement !== document.body &&
           cnt((cur.parentElement.innerText || '').replace(BIDI, '')) === 1) cur = cur.parentElement;
    if (!opts.includes(cur)) opts.push(cur);
  }
  const options = opts.map((el, i) => {
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
    return {i, raw: ser(el, false), attrs: parts.join(' ')};
  });
  let root = opts.length ? opts[0] : document.body;
  while (opts.length && root.parentElement && root !== document.body) {
    const t = (root.innerText || '').replace(BIDI, '');
    if (opts.every(o => root.contains(o)) && cntRe.test(t)) break;
    root = root.parentElement;
  }
  return {root: ser(root, true), options};
}
"""
JS_COUNTER_CHANGED = r"""
(cur) => {
  const t = (document.body.innerText || '').replace(/[\u200b-\u200f\u202a-\u202e\u2066-\u2069]/g, '');
  const m = t.match(/(?:^|\n)\s*(\d+)\s*\/\s*\d+\s*(?:\n|$)/);
  return !!m && parseInt(m[1], 10) !== cur;
}
"""
JS_QUIZ_PRESENT = r"""
() => {
  const t = (document.body ? (document.body.innerText || '') : '').replace(/[\u200b-\u200f\u202a-\u202e\u2066-\u2069]/g, '');
  return /(?:^|\n)\s*\d+\s*\/\s*\d+\s*(?:\n|$)/.test(t);
}
"""
JS_BODY = "document.body ? (document.body.innerText || '') : ''"

# ───────────────────────── تنظيف النص ─────────────────────────
_SUP = str.maketrans("0123456789+-−=()n", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁻⁼⁽⁾ⁿ")
_SUB = str.maketrans("0123456789+-−=()", "₀₁₂₃₄₅₆₇₈₉₊₋₋₌₍₎")
_TEX = {r"\times": "×", r"\cdot": "·", r"\leq": "≤", r"\le": "≤", r"\geq": "≥", r"\ge": "≥",
        r"\pm": "±", r"\mu": "µ", r"\%": "%", r"\approx": "≈", r"\neq": "≠", r"\ne": "≠",
        r"\rightarrow": "→", r"\to": "→", r"\div": "÷", r"\alpha": "α", r"\beta": "β",
        r"\gamma": "γ", r"\delta": "δ", r"\Delta": "Δ", r"\circ": "°", r"\lt": "<", r"\gt": ">",
        r"\,": " ", r"\;": " ", r"\:": " ", r"\ ": " ", r"\!": "", r"\left": "", r"\right": "",
        r"\_": "_", r"\&": "&", r"\$": "$", r"\#": "#"}


def _script(m, table, mark):
    inner = m.group(1).strip()
    if inner and all(ord(c) in table for c in inner):
        return inner.translate(table)
    return f"{mark}({inner})"


def script_conv(s: str) -> str:
    s = re.sub(r"\^\{([^{}]*)\}", lambda m: _script(m, _SUP, "^"), s)
    return re.sub(r"_\{([^{}]*)\}", lambda m: _script(m, _SUB, "_"), s)


def _frac(m):
    a, b = m.group(1), m.group(2)
    simple = lambda x: re.fullmatch(r"[\w.]+", x)
    return f"{a}/{b}" if simple(a) and simple(b) else f"({a})/({b})"


def tex_to_text(t: str) -> str:
    t = t.strip().strip("$")
    for _ in range(4):
        t = re.sub(r"\\(?:text|mathrm|textbf|mathbf|operatorname|mathit)\{([^{}]*)\}", r"\1", t)
    for _ in range(3):
        t = re.sub(r"\\d?frac\{([^{}]*)\}\{([^{}]*)\}", _frac, t)
    t = re.sub(r"\\sqrt\{([^{}]*)\}", r"√(\1)", t)
    for k in sorted(_TEX, key=len, reverse=True):
        t = t.replace(k, _TEX[k])
    t = re.sub(r"\^([A-Za-z0-9+\-−])", r"^{\1}", t)
    t = re.sub(r"_([A-Za-z0-9])", r"_{\1}", t)
    t = script_conv(t)
    t = t.replace("{", "").replace("}", "")
    t = re.sub(r"\\([A-Za-z]+)", r"\1", t)
    return t


def prep(raw: str) -> str:
    s = INVISIBLE.sub("", raw or "").replace("\u00a0", " ")
    s = AR_PRES.sub(lambda m: unicodedata.normalize("NFKC", m.group(0)), s)
    s = re.sub(r"\x01(.*?)\x02", lambda m: tex_to_text(m.group(1)), s, flags=re.S)
    return script_conv(s)


_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
# قاموس تصحيحات يدوي قابل للتوسعة (كلمات تأتي مقطعة من مصدر الصفحة)
ARABIC_FIXES = {"محلو ل": "محلول"}
SENT_END = re.compile(r"[.؟?!:؛]$")


def fix_text(s: str) -> str:
    s = re.sub(r"[ \t]+", " ", s).strip()
    s = s.replace("\u2212", "-").replace("\u2009", " ")
    if os.environ.get("QUIZ_DIGITS", "keep") == "latin":
        s = s.translate(_DIGITS)
    for k, v in ARABIC_FIXES.items():
        s = s.replace(k, v)
    # حرف ل/ة منفصل عن كلمته عربيًا (تقطيع مصدر) → يُلصق
    s = re.sub(r"([\u0621-\u064A]{2,})\s+([لة])(?![\u0621-\u064A])", r"\1\2", s)
    s = re.sub(r"\s+([؟،؛.,;:!?)\]])", r"\1", s)          # لا مسافة قبل علامات الترقيم
    s = re.sub(r"([(\[])\s+", r"\1", s)                     # ولا بعد قوس الفتح
    s = re.sub(r"([\u0621-\u064A])(?=[A-Za-z0-9])", r"\1 ", s)  # عربي ملتصق بلاتيني/رقم
    s = re.sub(r"([A-Za-z0-9%)\]])(?=[\u0621-\u064A])", r"\1 ", s)
    return re.sub(r" {2,}", " ", s).strip()


def clean_line(s: str) -> str:
    return fix_text(s)


def clean_lines(raw: str) -> list[str]:
    out = []
    for ln in prep(raw).splitlines():
        s = clean_line(ln)
        if not s or ICON_LINE.match(s) or s.lower() in ICON_WORDS:
            continue
        out.append(s)
    return out


def _join(parts: list[str]) -> str:
    out = ""
    for p in parts:
        out = p if not out else (out + p if re.match(r"^[؟،؛.,;:!?)\]]", p) else out + " " + p)
    return out


def split_bilingual(lines: list[str]) -> dict:
    """يوزّع الأسطر إلى en/ar؛ يدمج الشظايا اللاتينية/الرقمية القصيرة داخل جملة عربية (مثل 0.1 N HCl)."""
    kind = ["ar" if AR_CHAR.search(l) else "en" for l in lines]
    for i, l in enumerate(lines):
        if kind[i] != "en" or len(l) > 40 or SENT_END.search(l) and len(l) > 25:
            continue
        prev_ar = i > 0 and kind[i - 1] == "ar" and not SENT_END.search(lines[i - 1])
        next_ar = i + 1 < len(lines) and kind[i + 1] == "ar"
        if prev_ar and (next_ar or i == len(lines) - 1):
            kind[i] = "ar"
    en = [l for l, k in zip(lines, kind) if k == "en"]
    ar = [l for l, k in zip(lines, kind) if k == "ar"]
    return {"en": _join(en), "ar": _join(ar)}


def extra_lines(pre: list[str], post: list[str]) -> list[str]:
    out, j = [], 0
    for ln in post:
        if j < len(pre) and ln == pre[j]:
            j += 1
        else:
            out.append(ln)
    return out


# ───────────────────────── قراءة الحالة ─────────────────────────
def read_state(fr):
    st = fr.evaluate(JS_STATE)
    root = COUNTER_SPLIT.sub(r"\1 / \2", prep(st.get("root", "")))
    st["root_lines"] = [clean_line(l) for l in root.splitlines() if clean_line(l)]
    return st


def parse_state(st):
    cnt, stem, in_stem = None, [], False
    for ln in st["root_lines"]:
        if ln == OPT_MARK:
            break
        m = COUNTER_LINE.match(ln)
        if m and not in_stem:
            cnt, in_stem = (int(m.group(1)), int(m.group(2))), True
            continue
        if in_stem and not (ICON_LINE.match(ln) or ln.lower() in ICON_WORDS):
            stem.append(ln)
    return cnt, stem


def parse_option(raw: str):
    lines = clean_lines(raw)
    if not lines:
        return "?", [], []
    m = re.match(r"^\s*([A-F])\s*[.)]\s*(.*)$", lines[0])
    letter, first = (m.group(1), m.group(2).strip()) if m else ("?", lines[0])
    return letter, ([first] if first else []) + lines[1:], lines


def click_button(page, pattern: str, timeout=5000) -> bool:
    btn = page.get_by_role("button", name=re.compile(pattern, re.I))
    if btn.count() == 0:
        btn = page.get_by_text(re.compile(pattern, re.I))
    if btn.count() == 0:
        return False
    btn.first.click(timeout=timeout)
    return True


def capture_question(fr, reveal: bool, hints: bool) -> dict:
    s0 = read_state(fr)
    cnt, stem = parse_state(s0)
    q = {"number": cnt[0] if cnt else None, "total": cnt[1] if cnt else None,
         "stem": split_bilingual(stem), "hint": None, "options": [], "correct": []}
    if not s0["options"]:
        q["raw"] = [l for l in s0["root_lines"]]
        return q
    s1 = s0
    if hints:
        try:
            if click_button(fr, BTN_HINT, timeout=2500):
                time.sleep(0.8)
                s1 = read_state(fr)
                new = [l for l in extra_lines(s0["root_lines"], s1["root_lines"])
                       if l != OPT_MARK and not re.match(BTN_HINT + "|" + BTN_NEXT + "|" + BTN_PREV, l, re.I)]
                if new:
                    q["hint"] = split_bilingual(new)
        except Exception:
            s1 = read_state(fr)
    pre = []
    for o in s1["options"]:
        letter, body, full = parse_option(o["raw"])
        pre.append(full)
        q["options"].append({"letter": letter, **split_bilingual(body), "status": "", "explanation": {"en": "", "ar": ""}})
    if reveal:
        try:
            fr.locator('[data-qopt="0"]').first.click(timeout=5000)
            s2 = None
            for wait in (1.0, 1.4):
                time.sleep(wait)
                s2 = read_state(fr)
                if any(extra_lines(pre[o["i"]], clean_lines(o["raw"])) for o in s2["options"] if o["i"] < len(pre)):
                    break
            for o in s2["options"]:
                i = o["i"]
                if i >= len(pre):
                    continue
                expl = []
                for ln in extra_lines(pre[i], clean_lines(o["raw"])):
                    if VERDICT_OK.match(ln):
                        q["options"][i]["status"] = "correct"
                    elif VERDICT_BAD.match(ln):
                        q["options"][i]["status"] = "wrong"
                    else:
                        expl.append(ln)
                q["options"][i]["explanation"] = split_bilingual(expl)
            q["correct"] = [o["letter"] for o in q["options"] if o["status"] == "correct"]
            if not q["correct"]:
                q["correct"] = [q["options"][o["i"]]["letter"] for o in s2["options"]
                                if o["i"] < len(q["options"]) and CORRECT_ATTR.search(o["attrs"] or "")]
                for o in q["options"]:
                    if o["letter"] in q["correct"]:
                        o["status"] = "correct"
        except Exception as e:
            q["error"] = f"تعذّر كشف الإجابة: {type(e).__name__}"
    return q


# ───────────────────────── التصدير ─────────────────────────
def render_txt(questions: list[dict]) -> str:
    out = []
    for q in questions:
        out.append(f"[{q.get('number')}/{q.get('total')}]")
        for k in ("en", "ar"):
            if q["stem"][k]:
                out.append(q["stem"][k])
        if q.get("hint"):
            out.append("تلميح / Hint:")
            out += [f"    {q['hint'][k]}" for k in ("en", "ar") if q["hint"][k]]
        out.append("")
        for o in q.get("options", []):
            mark = {"correct": "✓ ", "wrong": "✗ "}.get(o["status"], "  ")
            out.append(f"{mark}{o['letter']}. {o['en']}".rstrip())
            if o["ar"]:
                out.append(f"      {o['ar']}")
            if o["status"] == "correct":
                out.append("      ← الإجابة الصحيحة")
            elif o["status"] == "wrong":
                out.append("      ← إجابة غير صحيحة (الخيار المنقور)")
            for k in ("en", "ar"):
                if o["explanation"][k]:
                    out.append(f"      ↳ {o['explanation'][k]}")
        if q.get("correct"):
            out += ["", "الإجابة الصحيحة: " + ", ".join(q["correct"])]
        if q.get("raw"):
            out += ["(لم تُحدَّد الخيارات - النص الخام:)"] + [f"    {x}" for x in q["raw"]]
        out += ["", "-" * 40, ""]
    return "\n".join(out)


def build_json(questions: list[dict], url: str, base: str, reveal: bool) -> dict:
    out, warn_total = [], 0
    for q in questions:
        opts, w = [], []
        for o in q.get("options", []):
            opts.append({"key": o["letter"],
                         "text": {"en": o["en"], "ar": o["ar"]},
                         "is_correct": o["letter"] in q.get("correct", []),
                         "explanation": o["explanation"]})
        keys = q.get("correct", [])
        ans = [o for o in opts if o["is_correct"]]
        if len(opts) < 2:
            w.append("options_lt_2")
        if reveal and not keys:
            w.append("no_correct_answer")
        if len(keys) > 1:
            w.append("multiple_correct")
        if not q["stem"]["en"] and not q["stem"]["ar"]:
            w.append("empty_question")
        if (q["stem"]["ar"] == "") != all(o["text"]["ar"] == "" for o in opts) and q["stem"]["ar"] == "":
            w.append("question_missing_ar")
        for o in opts:
            if q["stem"]["ar"] and not o["text"]["ar"] and AR_CHAR.search(o["text"]["en"]) is None \
                    and not re.fullmatch(r"[\d\s.,%+\-–×/^()<>=a-zA-Z²³¹⁰-⁹µ°]+", o["text"]["en"]):
                w.append(f"option_{o['key']}_missing_ar")
            if re.search(r"(?<![\u0621-\u064A])[\u0621-\u064A](?![\u0621-\u064A])",
                         o["text"]["ar"].replace("و", "")):
                w.append(f"option_{o['key']}_lone_arabic_letter")
        if reveal and opts and any(not (o["explanation"]["en"] or o["explanation"]["ar"]) for o in opts):
            w.append("missing_explanation")
        warn_total += len(w)
        out.append({
            "id": f"q{int(q.get('number') or 0):03d}",
            "number": q.get("number"),
            "question": q["stem"],
            "hint": q.get("hint") or {"en": "", "ar": ""},
            "options": opts,
            "answer": {"keys": keys,
                       "text": ans[0]["text"] if len(ans) == 1 else {"en": "", "ar": ""},
                       "explanation": ans[0]["explanation"] if len(ans) == 1 else {"en": "", "ar": ""}},
            "warnings": w,
        })
    total = questions[0].get("total") if questions else None
    return {"meta": {"schema_version": "2.0", "title": base, "source_url": url,
                     "extracted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                     "total_declared": total, "total_extracted": len(questions),
                     "answers_revealed": reveal, "languages": ["en", "ar"],
                     "warnings_count": warn_total},
            "questions": out}


# ───────────────────────── التصفح ─────────────────────────
def dismiss_overlays(page):
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


def current_counter(fr):
    t = COUNTER_SPLIT.sub(r"\1 / \2", prep(fr.evaluate(JS_BODY)))
    for ln in t.splitlines():
        m = COUNTER_LINE.match(ln)
        if m:
            return int(m.group(1)), int(m.group(2))
    return None


def main():
    if len(sys.argv) not in (3, 4):
        print("Usage: python extract_quiz.py <share_url> <output_base> [reveal=1|0]")
        sys.exit(1)
    url, base = sys.argv[1], sys.argv[2]
    reveal = (sys.argv[3] if len(sys.argv) == 4 else "1") not in ("0", "false", "False")
    hints = os.environ.get("QUIZ_HINTS", "1") != "0"
    headed = os.environ.get("QUIZ_HEADED") == "1"
    ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed,
                                    args=["--no-sandbox", "--disable-blink-features=AutomationControlled"])
        opts = {"viewport": {"width": 1100, "height": 1300}, "locale": "en-US", "timezone_id": "America/New_York"}
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
            debug_dump(page, "تمت إعادة التوجيه إلى تسجيل دخول Google؛ الرابط غير متاح للزائر." if "accounts.google.com" in page.url
                       else "لم تظهر صفحة الاختبار (عدّاد الأسئلة). راجع debug_quiz.png و debug_quiz.txt.")
            sys.exit(1)
        time.sleep(1.5)

        for _ in range(60):
            cnt = current_counter(fr)
            if not cnt or cnt[0] <= 1 or not click_button(fr, BTN_PREV):
                break
            try:
                fr.wait_for_function(JS_COUNTER_CHANGED, arg=cnt[0], timeout=8000)
            except Exception:
                break

        questions, seen = [], set()
        while True:
            q = capture_question(fr, reveal, hints)
            n, total = q.get("number"), q.get("total")
            if n in seen:
                break
            seen.add(n)
            questions.append(q)
            print(f"  التقط السؤال {n}/{total}: {(q['stem']['en'] or q['stem']['ar'])[:70]}")
            if not n or not total or n >= total:
                break
            try:
                if not click_button(fr, BTN_NEXT):
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
    data = build_json(questions, url, base, reveal)
    with open(f"{base}.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    wc = data["meta"]["warnings_count"]
    if wc:
        print(f"::warning::{wc} ملاحظة جودة في JSON (راجع warnings لكل سؤال).")
    print(f"Done. {len(questions)} questions -> {base}.txt / {base}.json")


if __name__ == "__main__":
    main()
