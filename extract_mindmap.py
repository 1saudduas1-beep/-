"""
يستخرج نص الخريطة الذهنية التفاعلية (رابط مشاركة NotebookLM) عبر Playwright إلى:
    <base>.json  مخطط منظَّم (meta + root + nodes) جاهز للاستيراد في تطبيق HTML لاحقاً
    <base>.txt   مخطط نصي متدرّج (Outline)

الاستراتيجيات (بالترتيب، ويُختار الأكمل):
  1) network : اعتراض استجابات الشبكة والتقاط شجرة JSON (name/children) من داخلها — الأدق إن وُجدت.
  2) dom     : توسيع كل العقد المطويّة تلقائياً ثم بناء الشجرة هندسياً (روابط SVG، وإلا أعمدة المستويات + تحليل الأب).

الاستخدام: python extract_mindmap.py <share_url> <base>
متغيرات اختيارية: MM_HEADED=1 ، MM_DEBUG=1 (يكتب debug_mindmap.* دائماً) ، MM_DIGITS=keep|latin
"""
import json
import math
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timezone

from playwright.sync_api import sync_playwright

AR = re.compile(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]")
AR_PRES = re.compile(r"[\uFB50-\uFDFF\uFE70-\uFEFF]")
INVIS = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff\u00ad]")
GLYPH = re.compile(r"[<>‹›«»+\-−–▶◀▸◂►◄⌃⌄]")
LABEL_KEYS = ("name", "title", "label", "text", "topic", "content")
CHILD_KEYS = ("children", "nodes", "items", "subtopics", "child")
OVERLAY_BUTTONS = [
    r"^\s*(Accept all|I agree|Agree|Reject all|Got it|Dismiss|No thanks|قبول الكل|موافق)\s*$",
    r"^\s*(Start|Get started|Let'?s go|ابدأ|بدء)\s*$",
]
EXPAND_ALL = r"expand|collapse|توسيع|وسّع|وسع|طي|فتح الكل"

# ───────────────────────── JS داخل الصفحة ─────────────────────────
JS_FRAGS = r"""
() => {
  const INV=/[\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff\u00ad]/g;
  const UI='button,[role=button],[role=toolbar],[role=dialog],[role=menu],nav,header,footer,mat-toolbar';
  const SKIPT=new Set(['SCRIPT','STYLE','NOSCRIPT','TITLE','TEMPLATE']);
  const ICON=/(material-icons|material-symbols|mat-icon|google-symbols)/;
  const frags=[];
  const tw=document.createTreeWalker(document.body||document.documentElement,NodeFilter.SHOW_TEXT);
  let n;
  while((n=tw.nextNode())){
    const p=n.parentElement; if(!p||SKIPT.has(p.tagName.toUpperCase())) continue;
    const raw=n.nodeValue.replace(INV,'').replace(/\s+/g,' ');
    if(!raw.trim()) continue;
    const cs=getComputedStyle(p);
    if(cs.display==='none'||cs.visibility==='hidden') continue;
    if(ICON.test(p.getAttribute('class')||'')) continue;
    const rg=document.createRange(); rg.selectNodeContents(n);
    let x0=1e9,y0=1e9,x1=-1e9,y1=-1e9,ok=false;
    for(const r of rg.getClientRects()){ if(r.width<=0||r.height<=0) continue; ok=true;
      x0=Math.min(x0,r.left);y0=Math.min(y0,r.top);x1=Math.max(x1,r.right);y1=Math.max(y1,r.bottom); }
    if(!ok) continue;
    frags.push({t:raw,x0,y0,x1,y1,sz:parseFloat(cs.fontSize)||14,ui:!!p.closest(UI)});
  }
  const links=[];
  document.querySelectorAll('svg path').forEach(p=>{try{
    const L=p.getTotalLength(); if(!(L>25)) return; const m=p.getScreenCTM(); if(!m) return;
    const a=p.getPointAtLength(0), b=p.getPointAtLength(L);
    const pa=new DOMPoint(a.x,a.y).matrixTransform(m), pb=new DOMPoint(b.x,b.y).matrixTransform(m);
    if(Math.abs(pa.x-pb.x)<12) return;
    links.push({ax:pa.x,ay:pa.y,bx:pb.x,by:pb.y});}catch(e){}});
  return {frags, links, title: document.title||'', vw: innerWidth, vh: innerHeight};
}
"""

JS_TOGGLES = r"""
() => {
  const M = window.__mm = window.__mm || {list: [], ids: new WeakMap()};
  const SKIP=/download|share|close|zoom|full ?screen|settings|more|feedback|copy|print|reset|fit|تحميل|مشاركة|إغلاق|تكبير|تصغير|ملء|إعادة/i;
  const rects=[]; const rtx=[]; const tw=document.createTreeWalker(document.body||document.documentElement,NodeFilter.SHOW_TEXT); let n;
  while((n=tw.nextNode())){ if(!n.nodeValue.trim()||rects.length>4000) continue; const p=n.parentElement; if(!p) continue;
    const t=p.tagName.toUpperCase(); if(t==='SCRIPT'||t==='STYLE') continue;
    const rg=document.createRange(); rg.selectNodeContents(n); const r=rg.getBoundingClientRect(); if(r.width>0&&r.height>0){ rects.push(r); rtx.push(n.nodeValue.trim().slice(0,60)); } }
  const cand=[];
  document.querySelectorAll('button,[role=button],div,span,g,circle,svg,a,i,mat-icon').forEach(e=>{
    const r=e.getBoundingClientRect(); if(r.width<9||r.height<9||r.width>46||r.height>46) return;
    const ar=r.width/r.height; if(ar<0.6||ar>1.7) return;
    const cs=getComputedStyle(e); const tag=e.tagName.toLowerCase();
    if(!(cs.cursor==='pointer'||tag==='button'||e.getAttribute('role')==='button')) return;
    const lab=(e.getAttribute('aria-label')||e.getAttribute('title')||''); if(SKIP.test(lab)) return;
    if((e.textContent||'').trim().length>24||e.querySelectorAll('*').length>10) return;
    const cx=r.left+r.width/2, cy=r.top+r.height/2;
    if(!rects.some(t=>cy>t.top-26&&cy<t.bottom+26&&(Math.abs(cx-t.right)<80||Math.abs(cx-t.left)<80))) return;
    cand.push(e); });
  const set=new Set(cand); const out=[];
  cand.forEach(e=>{ let p=e.parentElement, inner=false; while(p){ if(set.has(p)){inner=true;break} p=p.parentElement } if(inner) return;
    let id=M.ids.get(e); if(id===undefined){ id=M.list.length; M.list.push(e); M.ids.set(e,id); }
    const r=e.getBoundingClientRect();
    const sig=[e.getAttribute('aria-expanded')||'',e.getAttribute('aria-label')||'',(e.textContent||'').trim().slice(0,12),
      (e.innerHTML||'').replace(/\s+/g,'').slice(0,160),e.getAttribute('transform')||''].join('|');
    const cx=r.left+r.width/2, cy=r.top+r.height/2; let bi=-1, bd=1e9;
    rects.forEach((t,k)=>{ const d=Math.hypot(Math.max(t.left-cx,0,cx-t.right),Math.max(t.top-cy,0,cy-t.bottom)); if(d<bd){bd=d;bi=k} });
    out.push({i:id,sig,key:bi>=0?rtx[bi]:'',x:cx,y:cy}); });
  return out;
}
"""
JS_RECT = "(i)=>{const e=window.__mm&&window.__mm.list[i]; if(!e||!e.isConnected) return null; const r=e.getBoundingClientRect(); return {x:r.left+r.width/2,y:r.top+r.height/2,vw:innerWidth,vh:innerHeight}}"
JS_DISPATCH = ("(i)=>{const e=window.__mm.list[i]; if(!e) return false; const r=e.getBoundingClientRect();"
               "e.dispatchEvent(new MouseEvent('click',{bubbles:true,cancelable:true,view:window,clientX:r.left+r.width/2,clientY:r.top+r.height/2})); return true}")


# ───────────────────────── تنظيف النص ─────────────────────────
def clean(s: str) -> str:
    s = INVIS.sub("", s or "")
    if AR_PRES.search(s):
        s = unicodedata.normalize("NFKC", s)
    if os.environ.get("MM_DIGITS", "keep") == "latin":
        s = s.translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789"))
    return re.sub(r"\s+", " ", s).strip()


def split_colon(text: str):
    """يفصل «عنوان: وصف» عند أول نقطتين رأسيتين خارج الأقواس."""
    d = 0
    for i, ch in enumerate(text):
        d += (ch == "(") - (ch == ")")
        if ch == ":" and d == 0 and (i + 1 == len(text) or text[i + 1] == " ") and not (i and text[i - 1].isdigit()):
            return text[:i].strip(), text[i + 1:].strip()
    return text.strip(), ""


def _lang(s: str) -> str:
    return "ar" if len(AR.findall(s)) > len(re.findall(r"[A-Za-z]", s)) else "en"


def _top_groups(s: str):
    out, d, st = [], 0, 0
    for i, ch in enumerate(s):
        if ch == "(":
            if d == 0:
                st = i
            d += 1
        elif ch == ")" and d > 0:
            d -= 1
            if d == 0:
                out.append((st, i))
    return out


def _primary(s: str) -> str:
    """لغة النص الأساسي = لغة ما هو خارج الأقواس (الأقواس = الترجمة)."""
    out = s
    for a, b in reversed(_top_groups(s)):
        out = out[:a] + " " + out[b + 1:]
    return _lang(out) if re.search(r"[A-Za-z\u0600-\u06FF]", out) else ("en" if _lang(s) == "ar" else "ar")


def _trail(p: str, prim: str):
    """يفصل المجموعة الأخيرة بين قوسين إن كانت ترجمة (لغتها غير الأساسية وتنتهي بها الجملة)."""
    p = p.strip()
    g = _top_groups(p)
    if not g or g[-1][1] != len(p) - 1:
        return p, ""
    a, b = g[-1]
    head, inner = p[:a].strip(), p[a + 1:b].strip()
    if not head or not inner or _lang(inner) == prim:
        return p, ""
    if _lang(inner) == "ar":
        ok = len(AR.findall(inner)) >= 2
    else:  # مصطلح لاتيني داخل نص عربي مثل (II) أو (IIa) ليس ترجمة؛ الترجمة أطول
        ok = len(re.findall(r"[A-Za-z]", inner)) >= 6 or len(inner.split()) >= 2
    return (head, inner) if ok else (p, "")


def _place(main: str, tr: str) -> dict:
    if not main and not tr:
        return {"ar": "", "en": ""}
    ml = _lang(main) if re.search(r"[A-Za-z\u0600-\u06FF]", main) else ("ar" if _lang(tr) == "en" else "en")
    return {ml: main, ("ar" if ml == "en" else "en"): tr}


def parse_label(text: str) -> dict:
    """
    يدعم الصيغتين: «English (عربي): desc (عربي)» و«عربي (English): وصف (English)»،
    والترجمة الموحّدة «Title: desc (عنوان: وصف)». الأقواس الأخيرة = ترجمة الجملة التي قبلها.
    """
    prim = _primary(text)
    ttl, desc = split_colon(text)
    t_main, t_tr = _trail(ttl, prim)
    d_main, d_tr = _trail(desc, prim) if desc else ("", "")
    if desc and not t_tr and d_tr:
        a, b = split_colon(d_tr)
        if b:
            t_tr, d_tr = a, b
    return {"primary": prim, "title": _place(t_main, t_tr), "desc": _place(d_main, d_tr)}


def mk(text: str, depth: int) -> dict:
    text = clean(text)
    r = parse_label(text)
    t, d = r["title"], r["desc"]
    join = lambda k: ": ".join(x for x in (t[k], d[k]) if x)
    ar, en = join("ar"), join("en")
    return {"id": "", "depth": depth, "text": text, "primary": r["primary"], "ar": ar, "en": en,
            "bilingual": bool(ar and en), "title": t, "desc": d}


# ───────────────────────── الاستراتيجية 1: الشبكة ─────────────────────────
def _label(d):
    for k in LABEL_KEYS:
        if isinstance(d.get(k), str) and d[k].strip():
            return d[k]
    return None


def _kids(d):
    for k in CHILD_KEYS:
        if isinstance(d.get(k), list):
            return [c for c in d[k] if isinstance(c, dict)]
    return []


def _size(d, depth=0):
    return 1 + sum(_size(c, depth + 1) for c in _kids(d)) if depth < 30 else 1


def _walk(x, out, depth=0):
    if depth > 40:
        return
    if isinstance(x, str):
        s = x.strip()
        if len(s) > 20 and s[0] in "[{":
            try:
                _walk(json.loads(s), out, depth + 1)
            except Exception:
                pass
    elif isinstance(x, list):
        for i in x:
            _walk(i, out, depth + 1)
    elif isinstance(x, dict):
        if _label(x) and _kids(x):
            out.append(x)
        for v in x.values():
            _walk(v, out, depth + 1)


def find_tree(bodies):
    best, url = None, ""
    for u, body in bodies:
        docs = []
        b = body.lstrip()
        if b.startswith(")]}'"):
            b = b[4:]
        for cand in [b] + b.splitlines():
            cand = cand.strip()
            if cand[:1] in "[{":
                try:
                    docs.append(json.loads(cand))
                except Exception:
                    pass
        out = []
        for d in docs:
            _walk(d, out)
        for d in out:
            if best is None or _size(d) > _size(best):
                best, url = d, u
    return (best, url) if best is not None and _size(best) >= 4 else (None, "")


def from_json(d, depth=0):
    n = mk(_label(d) or "", depth)
    n["children"] = [from_json(c, depth + 1) for c in _kids(d) if _label(c)]
    return n


# ───────────────────────── الاستراتيجية 2: DOM هندسي ─────────────────────────
def cluster(frags):
    n = len(frags)
    par = list(range(n))

    def find(a):
        while par[a] != a:
            par[a] = par[par[a]]
            a = par[a]
        return a
    for i in range(n):
        a = frags[i]
        for j in range(i + 1, n):
            b = frags[j]
            h = max(1.0, min(a["y1"] - a["y0"], b["y1"] - b["y0"]))
            vo = (min(a["y1"], b["y1"]) - max(a["y0"], b["y0"])) / h
            gap = max(b["x0"] - a["x1"], a["x0"] - b["x1"])
            wrap = 0 <= b["y0"] - a["y1"] < 0.6 * h and abs(a["x0"] - b["x0"]) < 8 and j == i + 1
            if (vo > 0.6 and gap < max(8, 0.8 * a["sz"])) or wrap:
                par[find(j)] = find(i)
    groups = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    nodes = []
    for idx in groups.values():
        fs = [frags[i] for i in sorted(idx)]
        t = clean("".join(f["t"] for f in fs))
        if not re.search(r"[A-Za-z\u0600-\u06FF0-9]", t):
            continue
        x0, y0 = min(f["x0"] for f in fs), min(f["y0"] for f in fs)
        x1, y1 = max(f["x1"] for f in fs), max(f["y1"] for f in fs)
        nodes.append({"text": t, "x0": x0, "y0": y0, "x1": x1, "y1": y1, "yc": (y0 + y1) / 2})
    return nodes


def assign_levels(nodes):
    def groups(key):
        cols = []
        for v in sorted(set(round(n[key]) for n in nodes)):
            if cols and v - cols[-1][-1] <= 10:
                cols[-1].append(v)
            else:
                cols.append([v])
        return cols
    gl, gr = groups("x0"), groups("x1")
    key, cols = ("x0", gl) if len(gl) <= len(gr) else ("x1", gr)
    centers = [sum(c) / len(c) for c in cols]
    for n in nodes:
        n["col"] = min(range(len(centers)), key=lambda k: abs(centers[k] - n[key]))
    cnt = [sum(1 for n in nodes if n["col"] == k) for k in range(len(cols))]
    rev = cnt[0] != 1 and cnt[-1] == 1
    for n in nodes:
        n["depth"] = (len(cols) - 1 - n["col"]) if rev else n["col"]
    return len(cols)


def _near(nodes, px, py, lim=60):
    """أقرب عقدة أفقياً بشرط محاذاتها رأسياً لنقطة طرف الرابط (الروابط تنتهي عند منتصف العقدة)."""
    best, bd = None, lim
    for i, n in enumerate(nodes):
        if abs(py - n["yc"]) > max(14, (n["y1"] - n["y0"]) / 2 + 6):
            continue
        dx = max(n["x0"] - px, 0, px - n["x1"])
        if dx < bd:
            best, bd = i, dx
    return best


def dp_level(P, C):
    k, m, INF = len(P), len(C), 1e18
    pre = [0.0]
    for y in C:
        pre.append(pre[-1] + y)

    def cost(j, i, i2):
        if i2 == i:
            return 0.0
        mid, mean = (C[i] + C[i2 - 1]) / 2, (pre[i2] - pre[i]) / (i2 - i)
        return min(abs(mid - P[j]), abs(mean - P[j]))
    dp = [[INF] * (m + 1) for _ in range(k + 1)]
    bk = [[0] * (m + 1) for _ in range(k + 1)]
    dp[0][0] = 0.0
    for j in range(k):
        for i in range(m + 1):
            if dp[j][i] >= INF:
                continue
            for i2 in range(i, m + 1):
                c = dp[j][i] + cost(j, i, i2)
                if c < dp[j + 1][i2]:
                    dp[j + 1][i2], bk[j + 1][i2] = c, i
    res, i = [0] * m, m
    for j in range(k, 0, -1):
        i0 = bk[j][i]
        for t in range(i0, i):
            res[t] = j - 1
        i = i0
    return res


def link_parents(nodes, links):
    par = {}
    for L in links:
        a, b = _near(nodes, L["ax"], L["ay"]), _near(nodes, L["bx"], L["by"])
        if a is None or b is None or a == b:
            continue
        p, c = (a, b) if nodes[a]["depth"] < nodes[b]["depth"] else (b, a)
        if nodes[c]["depth"] == nodes[p]["depth"] + 1:
            par.setdefault(c, p)
    return par


def dom_tree(data, warns):
    fr = [f for f in data["frags"] if not GLYPH.fullmatch(f["t"].strip())]
    nonui = [f for f in fr if not f["ui"]]
    fr = nonui if len(nonui) >= 5 else fr
    nodes = cluster(fr)
    if len(data["links"]) >= 3:  # أبقِ فقط العقد المتصلة بروابط (يستبعد عنوان الصفحة ولافتات الواجهة)
        keep = {i for L in data["links"] for px, py in ((L["ax"], L["ay"]), (L["bx"], L["by"]))
                for i in [_near(nodes, px, py)] if i is not None}
        if len(keep) >= max(3, 0.6 * len(nodes)):
            nodes = [nodes[i] for i in sorted(keep)]
    if len(nodes) < 3:
        return None
    nl = assign_levels(nodes)
    if nl > 12:
        warns.append(f"عدد الأعمدة غير معتاد ({nl}): قد تحتوي النتيجة نصوص واجهة غير تابعة للخريطة.")
    par = link_parents(nodes, data["links"])
    by = {}
    for i, n in enumerate(nodes):
        by.setdefault(n["depth"], []).append(i)
    for d in sorted(by):
        if d == 0:
            continue
        C = sorted(by[d], key=lambda i: nodes[i]["yc"])
        if all(c in par for c in C):
            continue
        P = sorted(by.get(d - 1, []), key=lambda i: nodes[i]["yc"])
        if not P:
            continue
        res = dp_level([nodes[p]["yc"] for p in P], [nodes[c]["yc"] for c in C])
        for c, r in zip(C, res):
            par.setdefault(c, P[r])
    kids = {}
    for c, p in par.items():
        kids.setdefault(p, []).append(c)
    roots = sorted(by.get(0, []), key=lambda i: nodes[i]["yc"])

    def build(i, d):
        n = mk(nodes[i]["text"], d)
        n["children"] = [build(c, d + 1) for c in sorted(kids.get(i, []), key=lambda c: nodes[c]["yc"])]
        return n
    trees = [build(r, 0) for r in roots]
    if len(trees) == 1:
        return trees[0], len(par) / max(1, len(nodes) - 1)
    warns.append("وُجد أكثر من جذر؛ أُنشئ جذر افتراضي.")
    root = mk(data.get("title") or "Mind Map", 0)
    root["children"] = trees

    def shift(n):
        n["depth"] += 1
        for c in n["children"]:
            shift(c)
    for t in trees:
        shift(t)
    return root, len(par) / max(1, len(nodes) - 1)


def _count(fr):
    try:
        return sum(1 for f in fr.evaluate(JS_FRAGS)["frags"] if not f["ui"])
    except Exception:
        return 0


def _click(page, fr, i):
    r = fr.evaluate(JS_RECT, i)
    if not r:
        return False
    if 0 <= r["x"] < r["vw"] and 0 <= r["y"] < r["vh"]:
        try:
            ox = oy = 0
            if fr != page.main_frame:
                b = fr.frame_element().bounding_box()
                ox, oy = b["x"], b["y"]
            page.mouse.click(ox + r["x"], oy + r["y"])
            return True
        except Exception:
            pass
    return bool(fr.evaluate(JS_DISPATCH, i))


def expand_all(page, fr):
    before = _count(fr)  # زر «توسيع/طي الكل» (يتبدّل حسب الحالة): انقر وتحقق من الزيادة وإلا تراجع
    try:
        for b in fr.get_by_role("button", name=re.compile(EXPAND_ALL, re.I)).all()[:3]:
            if not b.is_visible():
                continue
            b.click(timeout=2000)
            page.wait_for_timeout(900)
            if _count(fr) < before:
                b.click(timeout=2000)
                page.wait_for_timeout(900)
            break
    except Exception:
        pass
    closed, opened, tried, attempts = set(), set(), set(), {}
    for _ in range(600):
        acted = False
        for c in fr.evaluate(JS_TOGGLES):  # يُعاد الجلب بعد كل نقرة (قد يُعاد بناء الـDOM)
            i, sig, k = c["i"], c["sig"], c["key"]
            if attempts.get(k, 0) >= 2 or sig in opened or (sig not in closed and k in tried):
                continue
            before = _count(fr)
            if not _click(page, fr, i):
                continue
            page.wait_for_timeout(450)
            after = _count(fr)
            attempts[k] = attempts.get(k, 0) + 1
            acted = True
            if sig not in closed:
                tried.add(k)
                if after > before:
                    closed.add(sig)
                elif after < before:  # كانت مفتوحة فطويتها: أعد فتحها
                    opened.add(sig)
                    for c2 in fr.evaluate(JS_TOGGLES):
                        if c2["key"] == k:
                            _click(page, fr, c2["i"])
                            break
                    page.wait_for_timeout(450)
            break
        if not acted:
            break
    return len(closed)


# ───────────────────────── الإخراج ─────────────────────────
def finalize(root):
    nodes = []

    def go(n, pid, order, prefix):
        n["id"] = prefix
        flat = {k: v for k, v in n.items() if k != "children"}
        flat.update({"parent": pid, "order": order, "is_leaf": not n["children"]})
        nodes.append(flat)
        for k, c in enumerate(n["children"]):
            go(c, n["id"], k, f"{prefix}.{k + 1}")
    go(root, None, 0, "1")
    return nodes


def render_txt(root):
    out = []

    def line(n, k):
        return ": ".join(x for x in (n["title"][k], n["desc"][k]) if x)

    def go(n):
        pad = "  " * n["depth"]
        en, ar = line(n, "en"), line(n, "ar")
        out.append(f"{pad}- {en or ar or n['text']}")
        if en and ar:
            out.append(f"{pad}  ↳ {ar}")
        for c in n["children"]:
            go(c)
    go(root)
    return "\n".join(out) + "\n"


def quality(nodes):
    bi = sum(1 for n in nodes if n["bilingual"])
    return {"bilingual_nodes": bi, "ar_only": sum(1 for n in nodes if n["ar"] and not n["en"]),
            "en_only": sum(1 for n in nodes if n["en"] and not n["ar"]),
            "primary_en": sum(1 for n in nodes if n["primary"] == "en"),
            "primary_ar": sum(1 for n in nodes if n["primary"] == "ar"),
            "untranslated_ids": [n["id"] for n in nodes if not n["bilingual"]][:60]}


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


def best_frame(page):
    best, score = None, 0
    for fr in list(page.frames):
        try:
            d = fr.evaluate(JS_FRAGS)
            s = sum(1 for f in d["frags"] if not f["ui"]) + 5 * len(d["links"])
            if s > score:
                best, score = fr, s
        except Exception:
            continue
    return best, score


def debug_dump(page, sniff, why, fr=None):
    print(f"::warning::{why}" if why else "::notice::debug")
    try:
        page.screenshot(path="debug_mindmap.png", full_page=True)
    except Exception:
        pass
    try:
        lines = [why or "", f"url: {page.url}", ""]
        for f in page.frames:
            lines.append(f"frame: {f.url[:160]}  frags={_count(f)}")
        lines.append("\nnetwork bodies (json-like):")
        for u, b in sniff.bodies:
            if b.lstrip()[:5] in (")]}'\n", ")]}'") or b.lstrip()[:1] in "[{":
                lines.append(f"  {len(b):>8}  {u[:150]}")
        lines.append("\n--- body text (first 2000) ---\n" + (page.inner_text("body")[:2000] if page else ""))
        open("debug_mindmap.txt", "w", encoding="utf-8").write("\n".join(lines))
        if fr is not None:
            open("debug_mindmap.html", "w", encoding="utf-8").write(fr.content()[:1_500_000])
    except Exception:
        pass


class Sniffer:
    def __init__(self):
        self.bodies = []

    def __call__(self, r):
        try:
            if r.request.resource_type in ("image", "font", "media", "stylesheet"):
                return
            body = r.text()
            if 20 < len(body) < 8_000_000:
                self.bodies.append((r.url, body))
        except Exception:
            pass


def main():
    if len(sys.argv) != 3:
        print("Usage: python extract_mindmap.py <share_url> <output_base>")
        sys.exit(1)
    url, base = sys.argv[1], sys.argv[2]
    headed = os.environ.get("MM_HEADED") == "1"
    debug = os.environ.get("MM_DEBUG") == "1"
    ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
    warns, sniff = [], Sniffer()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed,
                                    args=["--no-sandbox", "--disable-blink-features=AutomationControlled"])
        opts = {"viewport": {"width": 1600, "height": 1000}, "locale": "en-US", "timezone_id": "America/New_York"}
        if not headed:
            opts["user_agent"] = ua
        ctx = browser.new_context(**opts)
        ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
        ctx.on("response", sniff)
        page = ctx.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=90000)
        except Exception as e:
            print(f"::warning::تحذير أثناء التحميل: {type(e).__name__}")

        net_tree, net_url, fr, deadline = None, "", None, time.time() + 120
        while time.time() < deadline:
            if "accounts.google.com" in page.url:
                debug_dump(page, "تمت إعادة التوجيه إلى تسجيل دخول Google؛ الرابط غير متاح للزائر.", None)
                sys.exit(1)
            dismiss_overlays(page)
            net_tree, net_url = find_tree(sniff.bodies)
            fr, score = best_frame(page)
            if net_tree is not None or score >= 8:
                break
            time.sleep(2)
        time.sleep(2)
        if net_tree is None:
            net_tree, net_url = find_tree(sniff.bodies)

        result, method, conf = None, "", 1.0
        if net_tree is not None:
            result, method = from_json(net_tree), "network"
        if fr is None:
            fr, _ = best_frame(page)
        dom_res = None
        if fr is not None:
            try:
                left = expand_all(page, fr)
                time.sleep(1.0)
                dom_res = dom_tree(fr.evaluate(JS_FRAGS), warns)
                print(f"expand: learned_closed_signatures={left}")
            except Exception as e:
                warns.append(f"فشل استخراج DOM: {type(e).__name__}: {e}")
        if dom_res is not None:
            dtree, dconf = dom_res
            if result is None or len(finalize(dtree)) > 1.2 * len(finalize(result)):
                result, method, conf = dtree, "dom", dconf
        if result is None:
            debug_dump(page, "لم أجد نص الخريطة الذهنية (لا JSON في الشبكة ولا عناصر نصية). راجع debug_mindmap.*", fr)
            sys.exit(1)
        if method == "dom" and conf < 0.9:
            warns.append(f"ثقة ربط الآباء {conf:.0%}: راجع الهرمية يدوياً.")
        if debug or warns:
            debug_dump(page, "; ".join(warns), fr)
        title = result["text"]
        browser.close()

    nodes = finalize(result)
    meta = {"title": title, "source_url": url, "method": method, "extracted_at": datetime.now(timezone.utc).isoformat(),
            "node_count": len(nodes), "leaf_count": sum(1 for n in nodes if n["is_leaf"]),
            "max_depth": max(n["depth"] for n in nodes), "quality": quality(nodes), "warnings": warns}
    if method == "network":
        meta["network_source"] = net_url.split("?")[0][:200]
    with open(f"{base}.json", "w", encoding="utf-8") as f:
        json.dump({"schema": "mindmap-1.0", "meta": meta, "root": result, "nodes": nodes}, f, ensure_ascii=False, indent=2)
    with open(f"{base}.txt", "w", encoding="utf-8") as f:
        f.write(render_txt(result))
    q = meta["quality"]
    print(f"OK: {len(nodes)} عقدة، أقصى عمق {meta['max_depth']}، الطريقة: {method}، ثنائية اللغة: {q['bilingual_nodes']}")


if __name__ == "__main__":
    main()
