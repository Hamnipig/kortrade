#!/usr/bin/env python3
"""site/index.html + site/data/*.json  →  Artifact 퍼블리시용 단일 파일.

Artifact 는 doctype/html/head/body 스켈레톤을 퍼블리시 시점에 씌우므로 그 래퍼를 벗기고,
데이터를 window.__BOOTSTRAP__ 로 인라인한다(퍼블리시된 파일 fetch 가 막혀도 동작하도록).
GitHub Pages 용 site/index.html 은 그대로 둔다 — 소스는 하나다.

사용법: python scripts/build_artifact.py --data site/data --out build/artifact.html
"""
from __future__ import annotations
import argparse, json, re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=str(ROOT / "site" / "index.html"))
    ap.add_argument("--data", default=str(ROOT / "site" / "data"))
    ap.add_argument("--out", default=str(ROOT / "build" / "artifact.html"))
    a = ap.parse_args()

    html = Path(a.src).read_text(encoding="utf-8")
    title = re.search(r"<title>(.*?)</title>", html, re.S).group(1)
    fonts = "\n".join(re.findall(r'<link rel="(?:preconnect|stylesheet)"[^>]*>', html))
    style = re.search(r"<style>.*?</style>", html, re.S).group(0)
    body  = re.search(r"<body>(.*?)</body>", html, re.S).group(1).strip()

    boot = {}
    d = Path(a.data)
    for f in sorted(d.glob("*.json")):
        boot[f"data/{f.name}"] = json.loads(f.read_text(encoding="utf-8"))
    if "data/manifest.json" not in boot:
        print(f"경고: {d} 에 manifest.json 이 없습니다 — 빈 상태로 퍼블리시됩니다")

    out = Path(a.out); out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        f"<title>{title}</title>\n{fonts}\n{style}\n"
        f"<script>window.__BOOTSTRAP__={json.dumps(boot, ensure_ascii=False, separators=(',',':'))};</script>\n"
        f"{body}\n", encoding="utf-8")
    print(f"→ {out}  ({out.stat().st_size/1024:.1f} KB, 인라인 {len(boot)}개: {', '.join(boot)})")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
