"""Category icons for the SERP tiles: one flat SVG per catalog category, in the logo's palette.

ESCI has no product images, so each tile shows its category's icon. The keys below are the categories in
shared/configs/*.yml (`catalog.categories`); the app maps a category to its file the same way `slug` does here, and falls
back to other.svg. To get more granular (say, per subcategory), add keys here and map them in App.jsx's categoryIcon.

    python app/brand/category_icons.py      # writes app/frontend/public/categories/*.svg and prints a contact sheet path
"""
import re, sys, pathlib, tempfile
out = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else pathlib.Path(__file__).resolve().parents[1] / "frontend/public/categories"
out.mkdir(parents=True, exist_ok=True)
N, N6, L, P, O = "#1B3139", "#1B5162", "#FF3621", "#FABFBA", "#F9F7F4"
I = {
"Electronics": f'''<path d="M22 54 V46 a26 26 0 0 1 52 0 V54" fill="none" stroke="{N}" stroke-width="7" stroke-linecap="round"/>
<rect x="14" y="50" width="18" height="28" rx="5" fill="{N}"/><rect x="64" y="50" width="18" height="28" rx="5" fill="{N}"/>
<rect x="24" y="54" width="8" height="20" rx="3" fill="{L}"/><rect x="64" y="54" width="8" height="20" rx="3" fill="{L}"/>''',
"Computers & Accessories": f'''<rect x="20" y="22" width="56" height="38" rx="4" fill="{N}"/><rect x="26" y="28" width="44" height="26" rx="2" fill="{P}"/>
<rect x="30" y="34" width="20" height="4" rx="2" fill="{L}"/><rect x="30" y="42" width="30" height="4" rx="2" fill="{O}"/>
<path d="M14 64 H82 L78 72 H18 Z" fill="{N}"/><rect x="42" y="64" width="12" height="3" rx="1.5" fill="{N6}"/>''',
"Cell Phones & Accessories": f'''<rect x="30" y="14" width="36" height="68" rx="7" fill="{N}"/><rect x="35" y="22" width="26" height="48" rx="2" fill="{P}"/>
<circle cx="48" cy="76" r="2.8" fill="{L}"/><rect x="41" y="17" width="14" height="2.5" rx="1.25" fill="{N6}"/>
<rect x="39" y="30" width="18" height="10" rx="2" fill="{L}"/><rect x="39" y="44" width="18" height="4" rx="2" fill="{O}"/><rect x="39" y="52" width="12" height="4" rx="2" fill="{O}"/>''',
"Camera & Photo": f'''<rect x="34" y="22" width="20" height="10" rx="2" fill="{N}"/><rect x="14" y="30" width="68" height="46" rx="6" fill="{N}"/>
<circle cx="48" cy="53" r="17" fill="{L}"/><circle cx="48" cy="53" r="10" fill="{O}"/><circle cx="48" cy="53" r="5" fill="{N}"/>
<rect x="68" y="36" width="8" height="5" rx="1.5" fill="{P}"/>''',
"Home & Kitchen": f'''<rect x="44" y="24" width="8" height="6" rx="2" fill="{L}"/><rect x="20" y="30" width="56" height="7" rx="3.5" fill="{N}"/>
<path d="M22 40 H74 V66 a10 10 0 0 1 -10 10 H32 a10 10 0 0 1 -10 -10 Z" fill="{N}"/>
<rect x="10" y="44" width="12" height="6" rx="3" fill="{N}"/><rect x="74" y="44" width="12" height="6" rx="3" fill="{N}"/>
<rect x="22" y="50" width="52" height="6" fill="{L}"/>''',
"Tools & Home Improvement": f'''<g transform="rotate(-40 48 48)"><rect x="44" y="34" width="8" height="52" rx="3" fill="{N}"/>
<rect x="44" y="70" width="8" height="16" rx="3" fill="{L}"/><rect x="26" y="18" width="40" height="18" rx="3" fill="{N}"/>
<rect x="60" y="18" width="10" height="18" rx="2" fill="{P}"/></g>''',
"Clothing, Shoes & Jewelry": f'''<path d="M36 16 Q48 26 60 16 L82 28 L74 44 L66 40 V80 H30 V40 L22 44 L14 28 Z" fill="{N}"/>
<path d="M36 16 Q48 26 60 16" fill="none" stroke="{L}" stroke-width="4"/><rect x="30" y="52" width="36" height="7" fill="{L}"/>''',
"Beauty & Personal Care": f'''<path d="M38 36 V22 L56 14 V36 Z" fill="{L}"/><rect x="35" y="34" width="26" height="14" rx="2" fill="{P}"/>
<rect x="32" y="46" width="32" height="36" rx="4" fill="{N}"/><rect x="32" y="56" width="32" height="4" fill="{N6}"/>''',
"Health & Household": f'''<rect x="34" y="12" width="28" height="12" rx="3" fill="{L}"/><rect x="26" y="24" width="44" height="60" rx="7" fill="{N}"/>
<rect x="32" y="38" width="32" height="32" rx="3" fill="{O}"/><rect x="44" y="42" width="8" height="24" rx="2" fill="{L}"/><rect x="36" y="50" width="24" height="8" rx="2" fill="{L}"/>''',
"Sports & Outdoors": f'''<rect x="26" y="44" width="44" height="8" rx="3" fill="{N}"/>
<rect x="18" y="30" width="10" height="36" rx="3" fill="{L}"/><rect x="68" y="30" width="10" height="36" rx="3" fill="{L}"/>
<rect x="8" y="36" width="10" height="24" rx="3" fill="{N}"/><rect x="78" y="36" width="10" height="24" rx="3" fill="{N}"/>''',
"Toys & Games": f'''<rect x="18" y="56" width="60" height="22" rx="3" fill="{N}"/><circle cx="30" cy="56" r="5" fill="{N}"/><circle cx="48" cy="56" r="5" fill="{N}"/><circle cx="66" cy="56" r="5" fill="{N}"/>
<rect x="28" y="30" width="40" height="22" rx="3" fill="{L}"/><circle cx="40" cy="30" r="5" fill="{L}"/><circle cx="56" cy="30" r="5" fill="{L}"/>''',
"Baby": f'''<path d="M42 22 Q48 8 54 22 Z" fill="{P}"/><rect x="40" y="20" width="16" height="6" rx="2" fill="{P}"/>
<rect x="34" y="26" width="28" height="9" rx="3" fill="{L}"/><rect x="32" y="35" width="32" height="47" rx="9" fill="{N}"/>
<rect x="38" y="46" width="11" height="3" rx="1.5" fill="{O}"/><rect x="38" y="56" width="11" height="3" rx="1.5" fill="{O}"/><rect x="38" y="66" width="11" height="3" rx="1.5" fill="{O}"/>''',
"Pet Supplies": f'''<ellipse cx="48" cy="62" rx="17" ry="14" fill="{N}"/>
<ellipse cx="26" cy="44" rx="7" ry="9" fill="{L}"/><ellipse cx="40" cy="32" rx="7" ry="9" fill="{L}"/><ellipse cx="56" cy="32" rx="7" ry="9" fill="{L}"/><ellipse cx="70" cy="44" rx="7" ry="9" fill="{L}"/>''',
"Grocery & Gourmet Food": f'''<path d="M48 32 C34 24 18 32 20 52 C22 72 36 84 48 78 C60 84 74 72 76 52 C78 32 62 24 48 32 Z" fill="{L}"/>
<rect x="46" y="16" width="5" height="18" rx="2.5" fill="{N}"/><path d="M51 26 Q60 14 70 18 Q62 30 51 26 Z" fill="{N6}"/>
<ellipse cx="34" cy="48" rx="4" ry="7" fill="{P}"/>''',
"Automotive": f'''<path d="M10 64 V54 Q10 48 18 46 L30 44 L40 32 H62 L74 44 L84 46 Q88 48 88 54 V64 Z" fill="{N}"/>
<path d="M44 37 H52 V44 H36 Z" fill="{P}"/><path d="M56 37 H60 L67 44 H56 Z" fill="{P}"/>
<circle cx="28" cy="66" r="9" fill="{N}"/><circle cx="28" cy="66" r="4" fill="{O}"/><circle cx="70" cy="66" r="9" fill="{N}"/><circle cx="70" cy="66" r="4" fill="{O}"/>
<rect x="80" y="50" width="6" height="4" rx="1.5" fill="{L}"/>''',
"Office Products": f'''<g transform="rotate(45 48 48)"><rect x="40" y="10" width="16" height="10" rx="2" fill="{P}"/><rect x="40" y="20" width="16" height="4" fill="{N6}"/>
<rect x="40" y="24" width="16" height="44" fill="{L}"/><rect x="45" y="24" width="6" height="44" fill="{N}" opacity="0.15"/>
<path d="M40 68 H56 L48 86 Z" fill="{P}"/><path d="M45 79 H51 L48 86 Z" fill="{N}"/></g>''',
"Arts, Crafts & Sewing": f'''<rect x="24" y="14" width="48" height="10" rx="3" fill="{N}"/><rect x="24" y="72" width="48" height="10" rx="3" fill="{N}"/>
<rect x="30" y="24" width="36" height="48" fill="{L}"/>
<g stroke="{P}" stroke-width="2.5"><line x1="30" y1="34" x2="66" y2="34"/><line x1="30" y1="44" x2="66" y2="44"/><line x1="30" y1="54" x2="66" y2="54"/><line x1="30" y1="64" x2="66" y2="64"/></g>
<path d="M66 60 Q82 66 80 86" fill="none" stroke="{L}" stroke-width="2.5" stroke-linecap="round"/>''',
"Books & Media": f'''<rect x="18" y="62" width="60" height="14" rx="2" fill="{N}"/><rect x="72" y="64" width="4" height="10" fill="{O}"/>
<rect x="24" y="46" width="54" height="14" rx="2" fill="{L}"/><rect x="72" y="48" width="4" height="10" fill="{O}"/>
<rect x="20" y="30" width="52" height="14" rx="2" fill="{N6}"/><rect x="66" y="32" width="4" height="10" fill="{O}"/>
<rect x="30" y="35" width="20" height="4" rx="2" fill="{P}"/>''',
"_other":  # fallback for a missing or unknown category
 f'''<path d="M18 34 L48 22 L78 34 V68 L48 80 L18 68 Z" fill="{N}"/><path d="M18 34 L48 46 L78 34 L48 22 Z" fill="{N6}"/>
<path d="M48 46 V80" stroke="{O}" stroke-width="1.5"/><path d="M32 28 L62 40 V52 L68 50 V38 L38 26 Z" fill="{L}"/>''',
}
def slug(c): return re.sub(r"[^a-z0-9]+", "-", c.lower()).strip("-") or "other"
for c, body in I.items():
    (out / f"{slug(c)}.svg").write_text(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 96 96">{body}</svg>\n')
html = "".join(f'<figure><div><img src="file://{out}/{slug(c)}.svg"></div><figcaption>{c}</figcaption></figure>' for c in I)
sheet = pathlib.Path(tempfile.gettempdir()) / "category_icons_sheet.html"
sheet.write_text(f'''<style>body{{font:13px system-ui;background:#fff;display:grid;grid-template-columns:repeat(5,170px);gap:14px;padding:14px;margin:0}}
figure{{margin:0}}div{{aspect-ratio:1;background:#EEEDE9;border-radius:6px;display:grid;place-items:center}}img{{width:62%}}figcaption{{margin-top:4px;color:#1B3139}}</style>{html}''')
print(f"{len(I)} icons -> {out}\ncontact sheet: {sheet}")
