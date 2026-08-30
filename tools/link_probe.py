"""P2 任务 2.3 摸底探针：redaction 链接摧毁/先存后补复现脚本。

运行：WSL 侧 .venv/bin/python tools/link_probe.py
结论已记入 PLAN.md「P2 决策记录」：apply_redactions 摧毁重叠区链接注释，
redact 前 get_links + redact 后按原 rect 重插即恢复（save 后持久化）。
"""
import pymupdf


def key(l):
    return (l['kind'], round(l['from'].x0, 1), round(l['from'].y0, 1),
            l.get('uri'), l.get('page'), l.get('name', ''))


CASES = [
    ('../example/Topological Hall effect instigated in kagome Mn3–xSn due to '
     'Mn-deficit induced noncoplanar spin structure_Achintya Low.pdf', 0),
    ('../example/Topological Hall effect instigated in kagome Mn3–xSn due to '
     'Mn-deficit induced noncoplanar spin structure_Achintya Low.pdf', 2),
    ('../example/test_paper3.pdf', 1),
]

for src, pageno in CASES:
    doc = pymupdf.open(src)
    pg = doc[pageno]
    before = pg.get_links()
    # 模拟真实：只 redact 部分区域（页面上 70%），链接部分存活部分被删
    pg.add_redact_annot(pymupdf.Rect(pg.rect.x0, pg.rect.y0, pg.rect.x1,
                                     pg.rect.y0 + pg.rect.height * 0.7))
    pg.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE,
                        graphics=pymupdf.PDF_REDACT_LINE_ART_NONE)
    after = pg.get_links()
    ak = {key(l) for l in after}
    restored = 0
    fails = 0
    for l in before:
        if key(l) not in ak:
            try:
                pg.insert_link(l)
                restored += 1
            except Exception as e:
                fails += 1
                print('  fail:', type(e).__name__, e)
    doc.save('/tmp/_lk2.pdf', garbage=4, deflate=True)
    d2 = pymupdf.open('/tmp/_lk2.pdf')
    final = d2[pageno].get_links()
    tag = src.split('/')[-1][:24]
    print(f'{tag} p{pageno}: before={len(before)} after_redact={len(after)} '
          f'restored={restored} fails={fails} final={len(final)} '
          f'kinds={sorted(l["kind"] for l in final)}')
    names = {l['name'] for l in final if l['kind'] == 4 and l.get('name')}
    if names:
        dest = d2.resolve_names()
        ok = sum(1 for n in names if n in dest)
        print(f'  named dests resolvable: {ok}/{len(names)}')
    doc.close()
    d2.close()
