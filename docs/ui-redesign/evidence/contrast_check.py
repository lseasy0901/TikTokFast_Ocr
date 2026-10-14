def srgb_to_lin(c):
    c = c / 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

def lum(hexs):
    h = hexs.lstrip('#')
    r, g, b = (int(h[i:i+2], 16) for i in (0, 2, 4))
    return 0.2126*srgb_to_lin(r) + 0.7152*srgb_to_lin(g) + 0.0722*srgb_to_lin(b)

def ratio(a, b):
    la, lb = lum(a), lum(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)

BG   = "#090B10"   # 主背景
PANEL= "#171B24"   # 内容面板
GLASS= "#252B36"   # 玻璃基色
TXT  = "#E9F0FF"   # 主文字
SUB  = "#A8B4C7"   # 次级文字
ACC  = "#80BFFF"   # 强调色
OK   = "#64D9A0"   # 成功色

surfaces = [("主背景", BG), ("内容面板", PANEL), ("玻璃基色", GLASS)]
fgs = [("主文字", TXT), ("次级文字", SUB), ("强调色", ACC), ("成功色", OK)]

print(f"{'前景':<8}{'背景':<10}{'对比度':>8}  {'AA正文4.5':>10}{'AA大字3.0':>11}{'AAA7.0':>9}")
print("-" * 60)
for fn, f in fgs:
    for sn, s in surfaces:
        r = ratio(f, s)
        print(f"{fn:<8}{sn:<10}{r:>8.2f}  {'PASS' if r>=4.5 else 'FAIL':>10}{'PASS' if r>=3.0 else 'FAIL':>11}{'PASS' if r>=7.0 else 'FAIL':>9}")
    print()

print("=== 关键结论 ===")
print(f"主文字/主背景      {ratio(TXT,BG):.2f}")
print(f"次级文字/主背景    {ratio(SUB,BG):.2f}")
print(f"次级文字/玻璃基色  {ratio(SUB,GLASS):.2f}")
print(f"强调色/主背景      {ratio(ACC,BG):.2f}")
print(f"强调色/玻璃基色    {ratio(ACC,GLASS):.2f}")
