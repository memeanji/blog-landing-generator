# -*- coding: utf-8 -*-
"""BrowserAutomation 에서 self.xxx 로 부르는데 정의가 없는 것을 전수 검사."""
import ast, io, sys

sys.stdout.reconfigure(encoding="utf-8")
P = r"C:\Users\894플러스\blog_landing_generator\app\services\browser.py"
tree = ast.parse(io.open(P, encoding="utf-8").read())

cls = next(n for n in tree.body
           if isinstance(n, ast.ClassDef) and n.name == "BrowserAutomation")

defined = set()
for n in cls.body:
    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
        defined.add(n.name)
    elif isinstance(n, ast.Assign):
        for t in n.targets:
            if isinstance(t, ast.Name):
                defined.add(t.id)
    elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
        defined.add(n.target.id)

# __init__ 에서 self.x = ... 로 만드는 속성도 정의로 인정
for n in ast.walk(cls):
    if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) \
            and n.value.id == "self" and isinstance(n.ctx, ast.Store):
        defined.add(n.attr)

used = {}
for n in ast.walk(cls):
    if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) \
            and n.value.id == "self" and isinstance(n.ctx, ast.Load):
        used.setdefault(n.attr, n.lineno)

missing = {k: v for k, v in used.items() if k not in defined}
print(f"정의된 멤버 {len(defined)}개 · 참조 {len(used)}개")
if missing:
    print("\n❌ 정의가 없는 참조:")
    for k, ln in sorted(missing.items(), key=lambda x: x[1]):
        print(f"   line {ln}: self.{k}")
    sys.exit(1)
print("\n✅ 존재하지 않는 self.xxx 참조 없음")
