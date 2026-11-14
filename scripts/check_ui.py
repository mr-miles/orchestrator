"""Lint helper: syntax-check the web UI's inline JavaScript (no browser or Node needed)."""
import re
import sys
from pathlib import Path

import esprima

html = Path(__file__).resolve().parent.parent / "legion_orch/server/static/index.html"
js = re.search(r"<script>(.*)</script>", html.read_text(), re.S).group(1)
# esprima predates ES2020: neutralise ?? and ?. for parsing only.
try:
    esprima.parseScript(js.replace("??", "||").replace("?.", "."))
except esprima.Error as e:
    sys.exit(f"{html}: {e}")
print(f"{html.name}: JavaScript OK")
