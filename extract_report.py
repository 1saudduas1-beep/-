"""
يستخرج محتوى «التقرير» (Report / Briefing / Study guide) من رابط مشاركة NotebookLM عبر Playwright إلى:
    <base>.json  مخطط منظَّم بكتل (meta + outline + blocks) جاهز للعرض لاحقاً في تطبيق HTML

الاستراتيجيات (يُختار الأدق تلقائياً):
  1) dom     : تحديد حاوية التقرير (أصغر عنصر يضم >=85% من النص)، التمرير لتحميل المحتوى الكسول،
               ثم تسلسل DOM إلى كتل: heading / paragraph / list (متداخلة) / table / code / quote / hr / image
               مع الحفاظ على النص المختلط (عربي + إنجليزي) كما هو وبدون إعادة ترتيب بصري.
  2) network : التقاط نص Markdown الخام من استجابات الشبكة إن وُجد (الأدق للجداول والرسوم النصية)،
               ويُعتمد فقط إذا طابق محتوى الصفحة (تغطية كلمات >= 90%).

الاستخدام: python extract_report.py <share_url> <base>
متغيرات اختيارية: RPT_HEADED=1 ، RPT_DEBUG=1 (يكتب debug_report.* دائماً) ، RPT_DIGITS=keep|latin ،
                  RPT_STRATEGY=auto|dom|network
"""
import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timezone

from playwright.sync_api import sync_playwright

AR = re.compile(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]")
LAT = re.compile(r"[A-Za-z]")
AR_PRES = re.compile(r"[\uFB50-\uFDFF\uFE70-\uFEFF]")
INVIS = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff\u00ad]")
BOX = re.compile(r"[─━│┃┌┐└┘├┤┬┴┼╔╗╚╝╠╣╦╩╬═║▼▲◄►◀▶↓↑]")
BOX_STRONG = re.compile(r"[─━│┃┌┐└┘├┤┬┴┼╔╗╚╝╠╣╦╩╬═║]")
ICON_LINE = re.compile(r"^[a-z]+(?:_[a-z]+)+$")
OVERLAY_BUTTONS = [
    r"^\s*(Accept all|I agree|Agree|Reject all|Got it|Dismiss|No thanks|قبول الكل|موافق)\s*$",
    r"^\s*(Start|Get started|Let'?s go|ابدأ|بدء)\s*$",
]

# ───────────────────────── JS داخل الصفحة ─────────────────────────
JS_COMMON = r"""
const INV=/[\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff\u00ad]/g;
const ICON=/(^|\s)(material-icons|material-symbols|mat-icon|google-symbols|material-symbols-outlined|material-icons-outlined)(\s|$)/;
const HID=/(visually-hidden|live-announcer|sr-only|screen-reader|cdk-visually-hidden)/i;
const SKIPT=new Set(['SCRIPT','STYLE','NOSCRIPT','TEMPLATE','SVG','MAT-ICON','CANVAS','IFRAME','BUTTON','INPUT','SELECT','TEXTAREA','NAV','AUDIO','VIDEO']);
const SKIPR=new Set(['toolbar','dialog','menu','menubar','tooltip','navigation','alert','status']);
function skip(n){
  if(n.nodeType!==1) return false;
  const tag=n.tagName.toUpperCase();
  if(SKIPT.has(tag)) return true;
  const cls=(n.getAttribute('class')||'');
  if(ICON.test(cls)||HID.test(cls)) return true;
  if(n.getAttribute('aria-hidden')==='true'||n.hasAttribute('hidden')) return true;
  const r=n.getAttribute('role'); if(r&&SKIPR.has(r)) return true;
  const cs=getComputedStyle(n);
  if(cs.display==='none'||cs.visibility==='hidden') return true;
  return false;
}
"""

# يحدد حاوية التقرير: أصغر عنصر يضم >= 85% من إجمالي النص المرئي خارج الواجهة
JS_FIND_ROOT = r"""
() => {
""" + JS_COMMON + r"""
  const acc=new Map(); let total=0;
  function walk(n,chain){
    if(n.nodeType===3){
      const t=(n.nodeValue||'').replace(INV,'').trim(); if(!t) return;
      total+=t.length; for(const a of chain) acc.set(a,(acc.get(a)||0)+t.length); return;
    }
    if(n.nodeType!==1||skip(n)) return;
    const c2=chain.concat([n]);
    for(const c of n.childNodes) walk(c,c2);
  }
  const body=document.body; if(!body) return {total:0};
  walk(body,[]);
  if(!total) return {total:0};
  let root=body;
  for(;;){
    let best=null,bl=0;
    for(const c of root.children){const l=acc.get(c)||0; if(l>bl){bl=l;best=c}}
    if(best&&bl>=0.85*total) root=best; else break;
  }
  window.__rp=root;
  let sc=null;
  for(let e=root;e&&e!==document.documentElement;e=e.parentElement){
    const cs=getComputedStyle(e);
    if(e.scrollHeight>e.clientHeight+60&&/(auto|scroll)/.test(cs.overflowY)){sc=e;break;}
  }
  window.__rps=sc||document.scrollingElement||document.documentElement;
  const tag=root.tagName.toLowerCase();
  return {total,rootTag:tag,rootCls:(root.getAttribute('class')||'').slice(0,80),
          scrollH:window.__rps.scrollHeight,clientH:window.__rps.clientHeight};
}
"""

JS_TEXT_SCORE = r"""
() => {
""" + JS_COMMON + r"""
  let total=0;
  (function w(n){
    if(n.nodeType===3){total+=(n.nodeValue||'').replace(INV,'').trim().length;return}
    if(n.nodeType!==1||skip(n)) return;
    for(const c of n.childNodes) w(c);
  })(document.body||document.documentElement);
  return total;
}
"""

JS_SCROLL = r"""
(frac) => {
  const s=window.__rps; if(!s) return {done:true,y:0,h:0};
  const step=Math.max(200,Math.floor(s.clientHeight*frac));
  s.scrollTop=s.scrollTop+step;
  const done=s.scrollTop+s.clientHeight>=s.scrollHeight-4;
  return {done,y:s.scrollTop,h:s.scrollHeight};
}
"""
JS_SCROLL_TOP = "() => { const s=window.__rps; if(s) s.scrollTop=0; return true; }"

# تسلسل DOM إلى كتل. الرموز: \x01..\x02 = LaTeX ، \x03..\x04 = أُسّ ، \x05..\x06 = رمز سفلي
JS_SERIALIZE = r"""
() => {
""" + JS_COMMON + r"""
  const root=window.__rp; if(!root) return {blocks:[],title:document.title||''};
  const BLOCKT=new Set(['ADDRESS','ARTICLE','ASIDE','BLOCKQUOTE','DD','DETAILS','DIV','DL','DT','FIELDSET','FIGCAPTION','FIGURE','FOOTER','FORM','H1','H2','H3','H4','H5','H6','HEADER','HR','LI','MAIN','OL','P','PRE','SECTION','SUMMARY','TABLE','UL','CENTER']);
  const PRE_WS=/^pre/;
  const esc=s=>s.replace(/([\\`*_\[\]<>])/g,'\\$1');
  function isBlock(n){
    if(n.nodeType!==1) return false;
    const tag=n.tagName.toUpperCase();
    if(tag==='BR') return false;
    const d=getComputedStyle(n).display;
    if(d==='contents') return false;
    if(BLOCKT.has(tag)) return true;
    const r=n.getAttribute('role');
    if(r&&/^(heading|list|listitem|table|grid|row|paragraph|code)$/.test(r)) return true;
    return !(d==='inline'||d==='inline-block'||d==='inline-flex'||d==='ruby'||d==='inline-grid');
  }
  function hasBlockKid(n){
    for(const c of n.children){
      if(skip(c)) continue;
      if(isBlock(c)) return true;
      if(getComputedStyle(c).display==='contents'&&hasBlockKid(c)) return true;
    }
    return false;
  }
  // [plain, markdown]
  function inl(n,pre){
    if(n.nodeType===3){
      let v=(n.nodeValue||'').replace(INV,'').replace(/\u00a0/g,' ');
      if(!pre) v=v.replace(/\s+/g,' ');
      return [v,esc(v)];
    }
    if(n.nodeType!==1||skip(n)) return ['',''];
    const tag=n.tagName.toUpperCase();
    if(tag==='BR') return ['\n','\n'];
    if(tag==='IMG'){const a=(n.getAttribute('alt')||'').trim();return [a,a]}
    if(n.classList.contains('katex')){
      const a=n.querySelector('annotation[encoding="application/x-tex"]');
      if(a){const t='\u0001'+a.textContent+'\u0002';return [t,t]}
    }
    const cs=getComputedStyle(n);
    const p2=pre||PRE_WS.test(cs.whiteSpace);
    let t='',m='';
    for(const c of n.childNodes){const [a,b]=inl(c,p2);t+=a;m+=b;}
    if(!t.trim()) return [t,m];
    if(tag==='SUP'){t='\u0003'+t+'\u0004';m='\u0003'+m+'\u0004';return [t,m]}
    if(tag==='SUB'){t='\u0005'+t+'\u0006';m='\u0005'+m+'\u0006';return [t,m]}
    const fw=parseInt(cs.fontWeight)||400;
    const bold=tag==='B'||tag==='STRONG'||(fw>=600&&/^(SPAN|FONT|A|EM|I|U|MARK)$/.test(tag));
    const ital=tag==='I'||tag==='EM'||(cs.fontStyle==='italic'&&/^(SPAN|FONT|A|B|STRONG)$/.test(tag));
    const code=tag==='CODE'||tag==='KBD'||tag==='SAMP';
    const lead=m.match(/^\s*/)[0], trail=m.match(/\s*$/)[0], core=m.trim();
    let w=core;
    if(code) w='`'+t.trim()+'`';
    if(bold&&!code) w='**'+w+'**';
    if(ital&&!code) w='*'+w+'*';
    if(tag==='A'&&n.getAttribute('href')&&!/^javascript:/i.test(n.getAttribute('href'))) w='['+w+']('+n.getAttribute('href')+')';
    return [t,lead+w+trail];
  }
  function inlineOf(n,pre){let t='',m='';for(const c of n.childNodes){const [a,b]=inl(c,pre);t+=a;m+=b;}return [t,m];}
  function isMono(cs){return /(mono|courier|consolas|menlo|code)/i.test(cs.fontFamily)}
  function para(t,m,out,cs){
    if(!t.trim()) return;
    out.push({type:'paragraph',text:t,md:m,raw:!!(cs&&PRE_WS.test(cs.whiteSpace)),mono:!!(cs&&isMono(cs))});
  }
  function flushInline(buf,out,cs){
    if(!buf.length) return; let t='',m='';
    for(const n of buf){const [a,b]=inl(n,cs&&PRE_WS.test(cs.whiteSpace));t+=a;m+=b;}
    buf.length=0; para(t,m,out,cs);
  }
  function cellText(c){
    const parts=[];
    (function w(n){
      if(n.nodeType===3){const v=(n.nodeValue||'').replace(INV,'').replace(/\u00a0/g,' ').replace(/\s+/g,' ');if(v.trim()||parts.length) parts.push(v);return}
      if(n.nodeType!==1||skip(n)) return;
      if(n.tagName==='BR'){parts.push('\n');return}
      const blk=isBlock(n);
      if(blk&&parts.length&&!/\n$/.test(parts[parts.length-1])) parts.push('\n');
      if(/^(UL|OL)$/.test(n.tagName)){let i=0;for(const li of n.children){i++;const [t]=inlineOf(li,false);parts.push((n.tagName==='OL'?i+'. ':'• ')+t.trim()+'\n')}return}
      const [t]=(n.children.length===0||!hasBlockKid(n))?inlineOf(n,false):['',''];
      if(t||!hasBlockKid(n)) {parts.push(t);} else for(const k of n.childNodes) w(k);
      if(blk&&!/\n$/.test(parts[parts.length-1]||'')) parts.push('\n');
    })(c);
    let s=parts.join('').replace(/[ \t]+\n/g,'\n').replace(/\n{2,}/g,'\n').trim();
    let [,m]=inlineOf(c,false);
    m=m.replace(/\s+/g,' ').trim();
    return {t:s,m:s.indexOf('\n')>=0?s:m};
  }
  function table(el){
    const rows=[...el.querySelectorAll('tr,[role=row]')].filter(r=>!skip(r)&&r.closest('table,[role=table],[role=grid]')===el);
    const out=[];let hdr=false;
    rows.forEach((r,ri)=>{
      const cells=[...r.children].filter(c=>!skip(c)&&/^(TD|TH)$/.test(c.tagName)||(c.getAttribute&&/(cell|columnheader|rowheader|gridcell)/.test(c.getAttribute('role')||'')));
      if(!cells.length) return;
      const row=[];
      cells.forEach(c=>{
        const ct=cellText(c);row.push(ct);
        const span=Math.min(20,parseInt(c.getAttribute('colspan')||'1')||1);
        for(let k=1;k<span;k++) row.push({t:'',m:''});
      });
      if(ri===0&&(cells.every(c=>c.tagName==='TH'||/columnheader/.test(c.getAttribute('role')||''))||r.parentElement.tagName==='THEAD')) hdr=true;
      out.push(row);
    });
    const cap=el.querySelector('caption');
    return {type:'table',header:hdr,rows:out.map(r=>r.map(c=>c.t)),rows_md:out.map(r=>r.map(c=>c.m)),caption:cap?cap.textContent.replace(INV,'').trim():''};
  }
  function liContent(li){
    const o={t:'',m:'',children:[],extra:[]};let first=true;
    function add(n){
      if(n.nodeType===3){const [a,b]=inl(n,false);o.t+=a;o.m+=b;return}
      if(n.nodeType!==1||skip(n)) return;
      const tag=n.tagName.toUpperCase(),r=n.getAttribute('role')||'';
      if(tag==='UL'||tag==='OL'||r==='list'){o.children.push(list(n));return}
      if(tag==='TABLE'||r==='table'||r==='grid'){o.extra.push(table(n));return}
      if(tag==='PRE'){o.extra.push(code(n));return}
      if(isBlock(n)||getComputedStyle(n).display==='contents'){
        if(n.querySelector('ul,ol,table,pre,[role=list]')){for(const c of n.childNodes) add(c);return}
        const [a,b]=inlineOf(n,false);
        if(a.trim()){ if(o.t&&!/\s$/.test(o.t)){o.t+=' ';o.m+=' '} o.t+=a;o.m+=b; }
        return;
      }
      const [a,b]=inl(n,false);o.t+=a;o.m+=b;
    }
    for(const c of li.childNodes) add(c);
    o.t=o.t.replace(/\s+/g,' ').trim();o.m=o.m.replace(/\s+/g,' ').trim();
    return o;
  }
  function list(el){
    const tag=el.tagName.toUpperCase();
    const ls=getComputedStyle(el).listStyleType||'';
    let ordered=tag==='OL'||/(decimal|alpha|roman|arabic|persian|hebrew)/.test(ls);
    const kids=[...el.querySelectorAll('li,[role=listitem]')].filter(c=>!skip(c)&&c.closest('ul,ol,[role=list]')===el);
    const items=kids.map(liContent);
    return {type:'list',ordered,start:ordered?(parseInt(el.getAttribute('start')||'1')||1):1,items:items.map(i=>({t:i.t,m:i.m,children:i.children,extra:i.extra}))};
  }
  function code(el){
    let t=el.textContent.replace(INV,'').replace(/\u00a0/g,' ').replace(/^\n+|\n+$/g,'');
    const c=el.querySelector('code'),cl=((c&&c.getAttribute('class'))||'')+' '+(el.getAttribute('class')||'');
    const m=cl.match(/(?:language|lang)-([\w+#-]+)/);
    return {type:'code',text:t,lang:m?m[1]:''};
  }
  function blocks(el,out){
    const buf=[];
    const csEl=getComputedStyle(el);
    for(const c of el.childNodes){
      if(c.nodeType===3){buf.push(c);continue}
      if(c.nodeType!==1||skip(c)) continue;
      const tag=c.tagName.toUpperCase();
      const role=c.getAttribute('role')||'';
      const cs=getComputedStyle(c);
      if(cs.display==='contents'){ if(hasBlockKid(c)){flushInline(buf,out,csEl);blocks(c,out);} else buf.push(c); continue; }
      if(!isBlock(c)){buf.push(c);continue}
      flushInline(buf,out,csEl);
      block(c,out);
    }
    flushInline(buf,out,csEl);
  }
  function block(c,out){
    const tag=c.tagName.toUpperCase(),role=c.getAttribute('role')||'',cs=getComputedStyle(c);
    if(/^H[1-6]$/.test(tag)||role==='heading'){
      const lv=/^H[1-6]$/.test(tag)?parseInt(tag[1]):(parseInt(c.getAttribute('aria-level'))||2);
      const [t,m]=inlineOf(c,false); if(t.trim()) out.push({type:'heading',level:lv,text:t,md:m}); return;
    }
    if(tag==='UL'||tag==='OL'||role==='list'){out.push(list(c));return}
    if(tag==='TABLE'||role==='table'||role==='grid'){out.push(table(c));return}
    if(tag==='PRE'){const k=code(c);if(k.text.trim()) out.push(k);return}
    if(tag==='HR'){out.push({type:'hr'});return}
    if(tag==='IMG'){const s=c.getAttribute('src')||'';out.push({type:'image',alt:c.getAttribute('alt')||'',src:s.startsWith('data:')?'':s});return}
    if(tag==='BLOCKQUOTE'){
      const inner=[];blocks(c,inner);
      const t=inner.map(b=>b.text||'').filter(Boolean).join('\n');
      if(t.trim()) out.push({type:'quote',text:t,md:inner.map(b=>b.md||b.text||'').filter(Boolean).join('\n')});return;
    }
    if(tag==='FIGURE'||tag==='DETAILS'||tag==='DD'||tag==='DT'||hasBlockKid(c)){blocks(c,out);return}
    // كتلة ورقية بنص فقط
    const pre=PRE_WS.test(cs.whiteSpace);
    const [t,m]=inlineOf(c,pre);
    if(!t.trim()) { const im=c.querySelector('img'); if(im) out.push({type:'image',alt:im.getAttribute('alt')||'',src:(im.getAttribute('src')||'').startsWith('data:')?'':(im.getAttribute('src')||'')}); return; }
    if(pre&&isMono(cs)){out.push({type:'code',text:t.replace(/^\n+|\n+$/g,''),lang:''});return}
    out.push({type:'paragraph',text:t,md:m,raw:pre,mono:isMono(cs)});
  }
  const out=[];
  if(isBlock(root)&&!hasBlockKid(root)) block(root,out); else blocks(root,out);
  return {blocks:out,title:document.title||'',dirAttr:(root.closest('[dir]')||document.documentElement).getAttribute('dir')||''};
}
"""


# ───────────────────────── تنظيف النص ─────────────────────────
_SUP = str.maketrans("0123456789+-−=()n", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁻⁼⁽⁾ⁿ")
_SUB = str.maketrans("0123456789+-−=()", "₀₁₂₃₄₅₆₇₈₉₊₋₋₌₍₎")
_TEX = {r"\times": "×", r"\cdot": "·", r"\leq": "≤", r"\le": "≤", r"\geq": "≥", r"\ge": "≥",
        r"\pm": "±", r"\mu": "µ", r"\%": "%", r"\approx": "≈", r"\neq": "≠", r"\ne": "≠",
        r"\rightarrow": "→", r"\to": "→", r"\div": "÷", r"\alpha": "α", r"\beta": "β",
        r"\gamma": "γ", r"\delta": "δ", r"\Delta": "Δ", r"\circ": "°", r"\lt": "<", r"\gt": ">",
        r"\,": " ", r"\;": " ", r"\:": " ", r"\ ": " ", r"\!": "", r"\left": "", r"\right": "",
        r"\_": "_", r"\&": "&", r"\$": "$", r"\#": "#"}
_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")


def _script(inner: str, table, mark: str) -> str:
    inner = inner.strip()
    if inner and all(ord(c) in table for c in inner):
        return inner.translate(table)
    return f"{mark}({inner})"


def tex_to_text(t: str) -> str:
    t = t.strip().strip("$")
    for _ in range(4):
        t = re.sub(r"\\(?:text|mathrm|textbf|mathbf|operatorname|mathit)\{([^{}]*)\}", r"\1", t)
    for _ in range(3):
        t = re.sub(r"\\d?frac\{([^{}]*)\}\{([^{}]*)\}", lambda m: f"({m.group(1)})/({m.group(2)})", t)
    t = re.sub(r"\\sqrt\{([^{}]*)\}", r"√(\1)", t)
    for k in sorted(_TEX, key=len, reverse=True):
        t = t.replace(k, _TEX[k])
    t = re.sub(r"\^\{([^{}]*)\}", lambda m: _script(m.group(1), _SUP, "^"), t)
    t = re.sub(r"_\{([^{}]*)\}", lambda m: _script(m.group(1), _SUB, "_"), t)
    t = re.sub(r"\^([A-Za-z0-9+\-−])", lambda m: _script(m.group(1), _SUP, "^"), t)
    t = re.sub(r"_([A-Za-z0-9])", lambda m: _script(m.group(1), _SUB, "_"), t)
    t = t.replace("{", "").replace("}", "")
    return re.sub(r"\\([A-Za-z]+)", r"\1", t)


def markers(s: str) -> str:
    """يحوّل رموز JS الخاصة (LaTeX/أُسّ/سفلي) إلى نص عادي."""
    s = re.sub(r"\x01(.*?)\x02", lambda m: tex_to_text(m.group(1)), s, flags=re.S)
    s = re.sub(r"\x03(.*?)\x04", lambda m: _script(m.group(1), _SUP, "^"), s, flags=re.S)
    return re.sub(r"\x05(.*?)\x06", lambda m: _script(m.group(1), _SUB, "_"), s, flags=re.S)


def clean(s: str, keep_nl: bool = False) -> str:
    """تنظيف خفيف يحافظ على النص المختلط عربي/إنجليزي كما هو (بلا إعادة ترتيب أو لصق كلمات)."""
    s = INVIS.sub("", s or "").replace("\u00a0", " ").replace("\u2009", " ")
    if AR_PRES.search(s):
        s = AR_PRES.sub(lambda m: unicodedata.normalize("NFKC", m.group(0)), s)
    s = markers(s)
    if os.environ.get("RPT_DIGITS", "keep") == "latin":
        s = s.translate(_DIGITS)
    if keep_nl:
        def _ln(ln):
            ind = len(ln) - len(ln.lstrip(" "))
            return " " * ind + re.sub(r"[ \t]+", " ", ln.strip())
        s = "\n".join(_ln(ln) if keep_nl == "indent" else re.sub(r"[ \t]+", " ", ln).strip() for ln in s.split("\n"))
        return re.sub(r"\n{3,}", "\n\n", s).strip("\n")
    return re.sub(r"\s+", " ", s).strip()


def lang_info(s: str) -> dict:
    a, e = len(AR.findall(s)), len(LAT.findall(s))
    lang = "mixed" if a >= 3 and e >= 3 else ("ar" if a > e else "en" if e else "none")
    first = next((c for c in s if AR.match(c) or LAT.match(c)), "")
    return {"lang": lang, "dir": "rtl" if first and AR.match(first) else "ltr"}


def is_diagram_line(ln: str) -> bool:
    return bool(BOX_STRONG.search(ln)) or bool(re.search(r"[▼▲◄►]|(?:─|-){2,}>|<(?:─|-){2,}", ln) and BOX.search(ln))


# ───────────────────────── Markdown → كتل (للنص الخام ولفقرات pre-wrap) ─────────────────────────
LIST_RE = re.compile(r"^(\s*)([*+•\-–]|\d+[.)])\s+(.*)$")
TBL_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


def md_inline(s: str):
    """(plain, md) من نص Markdown بسيط: **bold** *italic* `code` [t](u)."""
    md = s.strip()
    t = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1", md)
    t = re.sub(r"\*\*(.+?)\*\*", r"\1", t)
    t = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"\1", t)
    t = re.sub(r"`([^`]+)`", r"\1", t)
    t = t.replace("*", "")  # نجوم يتيمة/غير متوازنة في المصدر
    return re.sub(r"\s+", " ", t).strip(), md


def split_row(ln: str):
    ln = ln.strip()
    if ln.startswith("|"):
        ln = ln[1:]
    if ln.endswith("|"):
        ln = ln[:-1]
    return [c.strip() for c in ln.split("|")]


def stack_deeper(stack, ind) -> bool:
    """هل البند الجديد أعمق من الجذر (أي متداخل) وليس بنداً جذرياً جديداً؟"""
    return bool(stack) and ind > stack[0][0]


def md_to_blocks(text: str) -> list:
    lines = [l.rstrip() for l in text.replace("\t", "    ").split("\n")]
    out, i, n = [], 0, len(lines)
    para = []

    def flush():
        if para:
            t, m = md_inline(" ".join(x.strip() for x in para))
            if t:
                out.append({"type": "paragraph", "text": t, "md": m})
            para.clear()

    while i < n:
        ln = lines[i]
        s = ln.strip()
        if not s:
            flush()
            i += 1
            continue
        if s.startswith("```"):
            flush()
            lang, j, buf = s[3:].strip(), i + 1, []
            while j < n and not lines[j].strip().startswith("```"):
                buf.append(lines[j])
                j += 1
            out.append({"type": "code", "text": "\n".join(buf).strip("\n"), "lang": lang})
            i = j + 1
            continue
        if is_diagram_line(ln):  # كتلة رسم نصي: تستمر عبر الأسطر الفارغة ما دام التالي رسماً/مزاحاً
            flush()
            j, buf = i, []
            while j < n:
                cur = lines[j]
                if cur.strip() and (is_diagram_line(cur) or (len(cur) - len(cur.lstrip()) >= 6 and not LIST_RE.match(cur))):
                    buf.append(cur)
                    j += 1
                elif not cur.strip():
                    k = j
                    while k < n and not lines[k].strip():
                        k += 1
                    if k < n and is_diagram_line(lines[k]) and k - j <= 2:
                        buf.extend([""] * (k - j))
                        j = k
                    else:
                        break
                else:
                    break
            ind = min((len(x) - len(x.lstrip()) for x in buf if x.strip()), default=0)
            out.append({"type": "code", "text": "\n".join(x[ind:] for x in buf).rstrip(), "lang": "diagram"})
            i = j
            continue
        if s.startswith(">"):
            flush()
            j, qb = i, []
            while j < n and lines[j].strip().startswith(">"):
                qb.append(re.sub(r"^\s*>\s?", "", lines[j]))
                j += 1
            out.append({"type": "quote", "blocks": md_to_blocks("\n".join(qb))})
            i = j
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", s)
        if m:
            flush()
            t, md = md_inline(m.group(2))
            out.append({"type": "heading", "level": len(m.group(1)), "text": t, "md": md})
            i += 1
            continue
        if re.fullmatch(r"(?:-{3,}|\*{3,}|_{3,})", s):
            flush()
            out.append({"type": "hr"})
            i += 1
            continue
        if s.startswith("|") and i + 1 < n and TBL_SEP.match(lines[i + 1]):
            flush()
            hdr, rows, j = split_row(s), [], i + 2
            while j < n and lines[j].strip().startswith("|"):
                rows.append(split_row(lines[j]))
                j += 1
            allr = [hdr] + rows
            out.append({"type": "table", "header": True, "rows": [[md_inline(c)[0] for c in r] for r in allr],
                        "rows_md": [[c for c in r] for r in allr], "caption": ""})
            i = j
            continue
        m = LIST_RE.match(ln)
        if m:
            flush()
            j, stack, root_items, ordered0, start0 = i, [], [], bool(re.match(r"\d", m.group(2))), 1
            if ordered0:
                start0 = int(re.match(r"\d+", m.group(2)).group())
            while j < n:
                cur = lines[j]
                mm = LIST_RE.match(cur)
                if mm and not is_diagram_line(cur):
                    ind, mark, body = len(mm.group(1).replace("\t", "    ")), mm.group(2), mm.group(3)
                    if root_items and not stack_deeper(stack, ind) and bool(re.match(r"\d", mark)) != ordered0:
                        break
                    t, md = md_inline(body)
                    item = {"t": t, "m": md, "children": [], "extra": [], "_ord": bool(re.match(r"\d", mark))}
                    while stack and stack[-1][0] >= ind:
                        stack.pop()
                    if not stack:
                        root_items.append(item)
                    else:
                        par = stack[-1][1]
                        par["children"].append({"type": "list", "ordered": item["_ord"], "start": 1, "items": []}) \
                            if not par["children"] else None
                        par["children"][-1]["items"].append(item)
                    stack.append((ind, item))
                    j += 1
                elif cur.strip() and stack and len(cur) - len(cur.lstrip()) > stack[-1][0] and not is_diagram_line(cur):
                    t, md = md_inline(cur)
                    stack[-1][1]["t"] += " " + t
                    stack[-1][1]["m"] += " " + md
                    j += 1
                else:
                    break
            out.append({"type": "list", "ordered": ordered0, "start": start0, "items": root_items})
            i = j
            continue
        para.append(ln)
        i += 1
    flush()
    return out


# ───────────────────────── تطبيع كتل DOM ─────────────────────────
def norm_item(it: dict) -> dict:
    t = clean(it.get("t", ""))
    m = clean(it.get("m", ""))
    out = {"text": t, "md": m or t, **lang_info(t), "children": [], "extra": []}
    for c in it.get("children", []):
        nb = norm_block(c)
        if nb:
            out["children"].extend(nb)
    for x in it.get("extra", []):
        nb = norm_block(x)
        if nb:
            out["extra"].extend(nb)
    if not out["text"] and not out["children"] and not out["extra"]:
        return {}
    return out


def clean_item_tree(items):
    r = []
    for it in items:
        ni = norm_item(it) if "text" not in it else it
        if ni:
            r.append(ni)
    return r


def norm_block(b: dict) -> list:
    t = b.get("type")
    if t == "heading":
        tx = clean(b["text"])
        return [{"type": "heading", "level": max(1, min(6, int(b.get("level") or 2))), "text": tx,
                 "md": clean(b.get("md", "")) or tx, **lang_info(tx)}] if tx else []
    if t == "paragraph":
        raw = b.get("raw") or ("\n" in b["text"].strip("\n"))
        tx = clean(b["text"], keep_nl="indent" if raw else False)
        if not tx or ICON_LINE.match(tx):
            return []
        if raw and "\n" in tx:  # نص Markdown/رسم خام مع الأسطر: أعد تحليله إلى كتل
            if b.get("mono") and not any(LIST_RE.match(l) for l in tx.split("\n")) and not any(l.lstrip().startswith(("#", "|")) for l in tx.split("\n")):
                return [{"type": "code", "text": tx, "lang": "diagram" if BOX.search(tx) else ""}]
            res = []
            for blk in md_to_blocks(tx):
                res.extend(norm_block(blk))
            return res
        if is_diagram_line(tx) and (len(BOX_STRONG.findall(tx)) >= 2 or len(tx.strip()) <= 2):
            return [{"type": "code", "text": tx, "lang": "diagram", "_from_para": True}]
        m = LIST_RE.match(tx)
        if m and not re.match(r"\d", m.group(2)):
            t2, md2 = md_inline(m.group(3))
            return [{"type": "list", "ordered": False, "start": 1,
                     "items": [{"text": clean(t2), "md": clean(md2), **lang_info(t2), "children": [], "extra": []}],
                     "_from_para": True}]
        md = clean(b.get("md", "")) or tx
        return [{"type": "paragraph", "text": tx, "md": md, **lang_info(tx)}]
    if t == "list":
        items = clean_item_tree(b.get("items", []))
        return [{"type": "list", "ordered": bool(b.get("ordered")), "start": b.get("start", 1), "items": items}] if items else []
    if t == "table":
        rows = [[clean(c, keep_nl=True) for c in r] for r in b.get("rows", [])]
        rows_md = [[clean(c) for c in r] for r in b.get("rows_md", [])] or rows
        if not rows:
            return []
        w = max(len(r) for r in rows)
        rag = any(len(r) != w for r in rows)
        rows = [r + [""] * (w - len(r)) for r in rows]
        rows_md = [r + [""] * (w - len(r)) for r in rows_md]
        hdr = bool(b.get("header")) or (len(rows) > 1)
        return [{"type": "table", "header": hdr, "rows": rows, "rows_md": rows_md, "caption": clean(b.get("caption", "")),
                 "cols": w, "ragged_fixed": rag}]
    if t == "code":
        tx = (b.get("text") or "").replace("\u00a0", " ")
        tx = INVIS.sub("", tx).rstrip()
        if not tx.strip():
            return []
        return [{"type": "code", "text": tx, "lang": b.get("lang") or ("diagram" if BOX.search(tx) else "")}]
    if t == "quote" and b.get("blocks") is not None:
        kids = []
        for c in b["blocks"]:
            kids.extend(norm_block(c))
        kids = post_process(kids)
        tx = all_text(kids)
        return [{"type": "quote", "text": tx, "md": render_md(kids).strip("\n"), "blocks": kids, **lang_info(tx)}] if tx else []
    if t == "quote":
        tx = clean(b["text"], keep_nl=True)
        return [{"type": "quote", "text": tx, "md": clean(b.get("md", ""), keep_nl=True) or tx, **lang_info(tx)}] if tx else []
    if t == "hr":
        return [{"type": "hr"}]
    if t == "image":
        return [{"type": "image", "alt": clean(b.get("alt", "")), "src": b.get("src", "")}]
    return []


def post_process(blocks: list) -> list:
    """يدمج أسطر الرسوم النصية المتجاورة في كتلة code واحدة، وفقرات القوائم الحرفية المتتالية في قائمة."""
    out = []
    for b in blocks:
        if out:
            p = out[-1]
            if b["type"] == "code" and p["type"] == "code" and b.get("_from_para") and p.get("_from_para") \
                    and b.get("lang") == "diagram" and p.get("lang") == "diagram":
                p["text"] += "\n" + b["text"]
                p["_from_para"] = True
                continue
            if b["type"] == "list" and p["type"] == "list" and b.get("_from_para") and p.get("_from_para") \
                    and b["ordered"] == p["ordered"]:
                p["items"].extend(b["items"])
                continue
        out.append(b)
    for b in out:
        b.pop("_from_para", None)
    return out


def assign_ids(blocks: list) -> list:
    outline = []
    for k, b in enumerate(blocks, 1):
        b["id"] = f"b{k:03d}"
        if b["type"] == "heading":
            outline.append({"id": b["id"], "level": b["level"], "text": b["text"]})
    return outline


# ───────────────────────── العرض (Markdown) ─────────────────────────
def md_items(items, ordered, start, depth, out):
    pad = "   " * depth if ordered else "  " * depth
    for k, it in enumerate(items):
        mark = f"{start + k}." if ordered else "-"
        out.append(f"{pad}{mark} {it['md'] or it['text']}".rstrip())
        for ch in it.get("children", []):
            if ch["type"] == "list":
                md_items(ch["items"], ch["ordered"], ch.get("start", 1), depth + 1, out)
        for ex in it.get("extra", []):
            out.extend(("  " * (depth + 1) + l) if l else l for l in render_md([ex]).rstrip("\n").split("\n"))


def render_md(blocks: list) -> str:
    out = []
    for b in blocks:
        t = b["type"]
        if t == "heading":
            out += ["#" * b["level"] + " " + b["text"], ""]
        elif t == "paragraph":
            out += [b["md"] or b["text"], ""]
        elif t == "list":
            md_items(b["items"], b["ordered"], b.get("start", 1), 0, out)
            out.append("")
        elif t == "table":
            rows = [[(c or "").replace("\n", " <br> ").replace("|", "\\|") for c in r] for r in b["rows_md"]]
            if not rows:
                continue
            if b.get("caption"):
                out += [f"**{b['caption']}**", ""]
            out.append("| " + " | ".join(rows[0]) + " |")
            out.append("|" + "|".join(["---"] * len(rows[0])) + "|")
            out += ["| " + " | ".join(r) + " |" for r in rows[1:]]
            out.append("")
        elif t == "code":
            out += ["```" + (b.get("lang") or ""), b["text"], "```", ""]
        elif t == "quote":
            out += ["> " + l for l in (b["md"] or b["text"]).split("\n")] + [""]
        elif t == "hr":
            out += ["---", ""]
        elif t == "image":
            out += [f"![{b.get('alt', '')}]({b.get('src', '')})", ""]
    return "\n".join(out).rstrip("\n") + "\n"


# ───────────────────────── جمع وتقييم ─────────────────────────
def all_text(blocks) -> str:
    parts = []

    def items(its):
        for it in its:
            parts.append(it["text"])
            for ch in it.get("children", []):
                if ch["type"] == "list":
                    items(ch["items"])
            for ex in it.get("extra", []):
                parts.append(all_text([ex]))

    for b in blocks:
        t = b["type"]
        if t in ("heading", "paragraph", "quote", "code"):
            parts.append(b["text"])
        elif t == "list":
            items(b["items"])
        elif t == "table":
            parts += [c for r in b["rows"] for c in r]
    return "\n".join(parts)


def words(s: str) -> set:
    return set(re.findall(r"[\w\u0600-\u06FF]{2,}", s.lower()))


def stats(blocks) -> dict:
    cnt, langs = {}, {"ar": 0, "en": 0, "mixed": 0}
    for b in blocks:
        cnt[b["type"]] = cnt.get(b["type"], 0) + 1
        if b.get("lang") in langs:
            langs[b["lang"]] += 1
    return {"block_counts": cnt, "text_blocks_by_lang": langs, "chars": len(all_text(blocks))}


def merge_snapshots(snaps: list) -> list:
    """يدمج لقطات متتالية (للمحتوى الكسول/المُفرَّغ) مع الحفاظ على الترتيب."""
    def sig(b):
        return json.dumps(b, ensure_ascii=False, sort_keys=True)[:400]
    base = list(snaps[0]) if snaps else []
    for s in snaps[1:]:
        index = {sig(b): i for i, b in enumerate(base)}
        last = -1
        for b in s:
            k = sig(b)
            if k in index:
                last = index[k]
                continue
            base.insert(last + 1, b)
            last += 1
            index = {sig(x): i for i, x in enumerate(base)}
    return base


# ───────────────────────── الشبكة (Markdown الخام) ─────────────────────────
class Sniffer:
    def __init__(self):
        self.bodies = []
        self.failed = []

    def __call__(self, r):
        try:
            if r.request.resource_type in ("image", "font", "media", "stylesheet"):
                return
            try:
                body = r.text()
            except Exception:
                body = r.body().decode("utf-8", "replace")
            if 200 < len(body) < 12_000_000:
                self.bodies.append((r.url, body))
        except Exception:
            try:
                if "batchexecute" in r.url or r.request.resource_type in ("xhr", "fetch"):
                    self.failed.append(r.request)
            except Exception:
                pass

    def replay(self, ctx):
        """يعيد طلب استجابات تعذّر التقاط جسمها (يحدث أحياناً مع batchexecute)."""
        n = 0
        for req in self.failed[:8]:
            try:
                hd = {k: v for k, v in req.headers.items()
                      if not k.startswith(":") and k.lower() not in ("content-length", "host", "cookie")}
                resp = ctx.request.fetch(req.url, method=req.method, headers=hd, data=req.post_data, timeout=60000)
                body = resp.text()
                if 200 < len(body) < 12_000_000:
                    self.bodies.append((req.url, body))
                    n += 1
            except Exception:
                continue
        return n


def _strings(x, out, depth=0):
    if depth > 40:
        return
    if isinstance(x, str):
        if len(x) > 300:
            out.append(x)
        s = x.strip()
        if len(s) > 20 and s[0] in "[{":
            try:
                _strings(json.loads(s), out, depth + 1)
            except Exception:
                pass
    elif isinstance(x, list):
        for i in x:
            _strings(i, out, depth + 1)
    elif isinstance(x, dict):
        for v in x.values():
            _strings(v, out, depth + 1)


def md_score(s: str) -> float:
    ls = s.split("\n")
    if len(ls) < 6:
        return 0
    mk = sum(1 for l in ls if re.match(r"^\s*(#{1,6}\s|[*\-•]\s|\d+[.)]\s|\|)", l))
    return mk / len(ls) * min(1.0, len(s) / 3000) + (0.5 if re.search(r"(?m)^#{1,3}\s", s) else 0)


def find_markdown(bodies):
    best, best_sc, best_url = "", 0.0, ""
    for u, body in bodies:
        b = body.lstrip()
        if b.startswith(")]}'"):
            b = b[4:]
        docs = []
        for cand in [b] + b.splitlines():
            cand = cand.strip()
            if cand[:1] in "[{":
                try:
                    docs.append(json.loads(cand))
                except Exception:
                    pass
        strs = []
        for d in docs:
            _strings(d, strs)
        if not docs and "\n" in body and len(body) > 600:
            strs.append(body)
        for s in strs:
            if "\n" not in s:
                continue
            sc = md_score(s)
            if sc > best_sc:
                best, best_sc, best_url = s, sc, u
    return (best, best_url) if best_sc >= 0.25 else ("", "")


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


def best_frame(page):
    best, score = None, 0
    for fr in list(page.frames):
        try:
            s = fr.evaluate(JS_TEXT_SCORE)
            if s > score:
                best, score = fr, s
        except Exception:
            continue
    return best, score


def debug_dump(page, sniff, why, fr=None, extra=None):
    print(f"::warning::{why}" if why else "::notice::debug")
    try:
        page.screenshot(path="debug_report.png", full_page=False)
    except Exception:
        pass
    try:
        lines = [why or "", f"url: {page.url}", ""]
        for f in page.frames:
            try:
                lines.append(f"frame: {f.url[:160]}  text={f.evaluate(JS_TEXT_SCORE)}")
            except Exception:
                lines.append(f"frame: {f.url[:160]}  (unreadable)")
        lines.append("\nnetwork bodies:")
        for u, b in (sniff.bodies if sniff else []):
            lines.append(f"  {len(b):>9}  {u[:150]}")
        if extra:
            lines.append("\n" + extra)
        lines.append("\n--- body text (first 2500) ---\n" + (page.inner_text("body")[:2500] if page else ""))
        open("debug_report.txt", "w", encoding="utf-8").write("\n".join(lines))
        if fr is not None:
            open("debug_report.html", "w", encoding="utf-8").write(fr.content()[:1_500_000])
    except Exception:
        pass


def collect_dom(fr, warns):
    """يحدد الحاوية، يمرّر لتحميل المحتوى الكسول، ويجمع لقطات الكتل ثم يدمجها."""
    info = fr.evaluate(JS_FIND_ROOT)
    if not info.get("total"):
        return [], "", info
    snaps, title = [], ""
    first = fr.evaluate(JS_SERIALIZE)
    title = first.get("title", "")
    snaps.append(first["blocks"])
    sh, ch = info.get("scrollH", 0), info.get("clientH", 0)
    if sh > ch + 60:
        fr.evaluate(JS_SCROLL_TOP)
        time.sleep(0.4)
        last_h = 0
        for _ in range(400):
            st = fr.evaluate(JS_SCROLL, 0.8)
            time.sleep(0.35)
            try:
                if fr.evaluate(JS_FIND_ROOT).get("total", 0) == 0:
                    break
                snaps.append(fr.evaluate(JS_SERIALIZE)["blocks"])
            except Exception:
                break
            if st["done"]:
                time.sleep(0.8)
                if fr.evaluate(JS_SCROLL, 0.8)["h"] == st["h"] == last_h or st["h"] == last_h:
                    break
            last_h = st["h"]
    # اللقطة الأخيرة بعد الاستقرار
    try:
        snaps.append(fr.evaluate(JS_SERIALIZE)["blocks"])
    except Exception:
        pass
    sizes = [len(json.dumps(s, ensure_ascii=False)) for s in snaps]
    biggest = max(range(len(snaps)), key=lambda i: sizes[i])
    merged = merge_snapshots([snaps[biggest]] + [s for i, s in enumerate(snaps) if i != biggest])
    if len(snaps) > 3 and len(json.dumps(merged, ensure_ascii=False)) > 1.05 * sizes[biggest]:
        warns.append("المحتوى مُفرَّغ/كسول: دُمجت لقطات التمرير.")
        # أعد الترتيب بدمج اللقطات بترتيبها الزمني (أدق ترتيباً من الأكبر أولاً)
        merged = merge_snapshots(snaps)
    return merged, title, info


def build_blocks_from_dom(raw_blocks):
    blocks = []
    for b in raw_blocks:
        blocks.extend(norm_block(b))
    return post_process(blocks)


def build_blocks_from_md(md_text):
    blocks = []
    for b in md_to_blocks(clean(md_text, keep_nl=True) if False else md_text.replace("\r\n", "\n")):
        blocks.extend(norm_block(b))
    return post_process(blocks)


def main():
    if len(sys.argv) != 3:
        print("Usage: python extract_report.py <share_url> <output_base>")
        sys.exit(1)
    url, base = sys.argv[1], sys.argv[2]
    headed = os.environ.get("RPT_HEADED") == "1"
    debug = os.environ.get("RPT_DEBUG") == "1"
    strategy = os.environ.get("RPT_STRATEGY", "auto")
    ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
    warns, sniff = [], Sniffer()
    dom_blocks, md_blocks, title, info, md_url, method = [], [], "", {}, "", ""

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed,
                                    args=["--no-sandbox", "--disable-blink-features=AutomationControlled"])
        opts = {"viewport": {"width": 1280, "height": 1000}, "locale": "en-US", "timezone_id": "America/New_York"}
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

        # انتظر ظهور النص ثم استقراره (لا يتغير حجمه لثلاث قراءات متتالية)
        fr, deadline, hist = None, time.time() + 120, []
        while time.time() < deadline:
            if "accounts.google.com" in page.url:
                debug_dump(page, sniff, "تمت إعادة التوجيه إلى تسجيل دخول Google؛ الرابط غير متاح للزائر.")
                sys.exit(1)
            dismiss_overlays(page)
            fr, score = best_frame(page)
            hist = (hist + [score])[-3:]
            if score >= 400 and len(hist) == 3 and max(hist) - min(hist) <= 5:
                break
            time.sleep(2)
        time.sleep(1.5)
        if fr is None:
            fr, _ = best_frame(page)

        if fr is not None and strategy in ("auto", "dom"):
            try:
                raw, title, info = collect_dom(fr, warns)
                dom_blocks = build_blocks_from_dom(raw)
            except Exception as e:
                warns.append(f"فشل استخراج DOM: {type(e).__name__}: {e}")
        if strategy in ("auto", "network"):
            md_text, md_url = find_markdown(sniff.bodies)
            if not md_text and sniff.failed and sniff.replay(ctx):
                md_text, md_url = find_markdown(sniff.bodies)
            if md_text:
                try:
                    md_blocks = build_blocks_from_md(md_text)
                except Exception as e:
                    warns.append(f"فشل تحليل Markdown الشبكة: {type(e).__name__}")

        cov = None
        blocks = dom_blocks
        method = "dom"
        if md_blocks:
            dw, mw = words(all_text(dom_blocks)), words(all_text(md_blocks))
            if not dw:
                blocks, method = md_blocks, "network"
            elif mw:
                cov = len(dw & mw) / len(dw)
                if strategy == "network" or (cov >= 0.9 and len(mw) >= 0.9 * len(dw)):
                    blocks, method = md_blocks, "network"
                elif cov >= 0.5:
                    warns.append(f"نص الشبكة لا يطابق الصفحة بالكامل (تغطية {cov:.0%}); اعتُمد DOM.")
        if not blocks or len(all_text(blocks)) < 80:
            debug_dump(page, sniff, "لم أجد محتوى التقرير (لا كتل نصية). راجع debug_report.*", fr,
                       json.dumps(info, ensure_ascii=False))
            sys.exit(1)

        hr_n = sum(1 for b in blocks if b["type"] == "hr")
        if hr_n and os.environ.get("RPT_HR", "drop") != "keep":
            blocks = [b for b in blocks if b["type"] != "hr"]
        # فحوصات الجودة
        st = stats(blocks)
        if not any(b["type"] == "heading" for b in blocks):
            warns.append("لا توجد عناوين: ربما الحاوية خاطئة أو التقرير نص مسطح.")
        if any(b["type"] == "code" and b.get("lang") == "diagram" for b in blocks) and method == "dom":
            warns.append("رسوم نصية: قد تفقد محاذاة المسافات إن لم تكن الكتلة preformatted في الصفحة.")
        if any(b["type"] == "table" and b.get("ragged_fixed") for b in blocks):
            warns.append("جدول بأعمدة غير متساوية: ملئت الخلايا الناقصة بفراغ.")
        if info and info.get("total") and method == "dom":
            ratio = st["chars"] / max(1, info["total"])
            if ratio < 0.8:
                warns.append(f"المستخرج {ratio:.0%} من نص الحاوية فقط: قد يكون هناك محتوى لم يُلتقط.")
        if debug or warns:
            debug_dump(page, sniff, "; ".join(warns), fr, json.dumps(info, ensure_ascii=False))
        browser.close()

    outline = assign_ids(blocks)
    ttl = next((b["text"] for b in blocks if b["type"] == "heading" and b["level"] == 1), "") \
        or next((b["text"] for b in blocks if b["type"] == "heading"), "") \
        or re.sub(r"\s*[-–|].*$", "", title or "") or base
    langs = lang_info(all_text(blocks))
    meta = {"title": ttl, "source_url": url, "method": method,
            "extracted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "page_title": re.sub(r"^\s*Gemini Notebook\s*[-–]\s*", "", title or "").strip(),
            "hr_dropped": hr_n if os.environ.get("RPT_HR", "drop") != "keep" else 0,
            "primary_dir": langs["dir"], "primary_lang": langs["lang"], **st, "headings": len(outline),
            "warnings": warns}
    if method == "network":
        meta["network_source"] = md_url.split("?")[0][:200]
    if cov is not None:
        meta["dom_network_coverage"] = round(cov, 3)
    with open(f"{base}.json", "w", encoding="utf-8") as f:
        json.dump({"schema": "report-1.0", "meta": meta, "outline": outline, "blocks": blocks},
                  f, ensure_ascii=False, indent=2)
    bc = st["block_counts"]
    print(f"OK: {len(blocks)} كتلة ({', '.join(f'{k}={v}' for k, v in sorted(bc.items()))})، "
          f"{meta['headings']} عنوان، الطريقة: {method}، {st['chars']} حرف")


if __name__ == "__main__":
    main()
