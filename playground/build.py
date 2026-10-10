"""Assemble the standalone playground: template.html with the Python module, the presets, and
the recorded decisions written into it, as one file that opens in any recent browser.

    python playground/build.py
"""

from __future__ import annotations

import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
VERSION = "0.1.0a1"


def script_literal(value: object) -> str:
    """JSON that is safe inside a <script> element."""
    return json.dumps(value, ensure_ascii=False).replace("</", "<\\/").replace("<!--", "<\\!--")


def main() -> None:
    html = (HERE / "template.html").read_text(encoding="utf-8")
    presets = json.loads((HERE / "presets.json").read_text(encoding="utf-8"))
    values = {
        "__VERSION__": VERSION,
        "__PRESETS_JSON__": script_literal(presets),
        "__SNIPPETS_JSON__": script_literal(json.loads((HERE / "snippets.json").read_text(encoding="utf-8"))),
        "__PLAYGROUND_PY__": script_literal((HERE / "playground.py").read_text(encoding="utf-8")),
        "__DECISIONS__": script_literal((HERE / "decisions.jsonl").read_text(encoding="utf-8")),
        # exregex.semantic records its observations beside the lockfile, in its own file.
        "__SEMANTIC_DECISIONS__": script_literal((HERE / "decisions.semantic-v1.jsonl").read_text(encoding="utf-8")),
    }
    for name, value in values.items():
        if name not in html:
            raise SystemExit(f"template.html has no {name}")
        html = html.replace(name, value)
    left = re.findall(r"__[A-Z_]+__", html)
    if left:
        raise SystemExit(f"unfilled placeholders: {sorted(set(left))}")
    out = HERE / "ex-regex-playground.html"
    out.write_text(html, encoding="utf-8", newline="\n")
    print(f"wrote {out.name}: {len(html.encode('utf-8')):,} bytes")


if __name__ == "__main__":
    main()
