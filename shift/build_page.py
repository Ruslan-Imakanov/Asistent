"""Builds shift/office.html (the private claude.ai page) from the shell + the game drawing code in web/office.js."""
from pathlib import Path

HERE = Path(__file__).resolve().parent
game_js = (HERE.parent / "web" / "office.js").read_text(encoding="utf-8")
start = game_js.index("// ---------- office layout ----------")
end = game_js.index("// ---------- panel ----------")
game = game_js[start:end]
shell = (HERE / "page_shell.html").read_text(encoding="utf-8")
(HERE / "office.html").write_text(shell.replace("/*GAME*/", game), encoding="utf-8")
print("built", HERE / "office.html")
