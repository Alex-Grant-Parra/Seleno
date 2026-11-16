"""Collects _('...') strings from the JavaScript for pybabel (see babel.cfg).

Babel's own JavaScript reader misses _() calls inside template literals and
inside another _() call's arguments, both common here. Every translated JS
string is a plain quoted literal as the first argument of _(), so a pattern
finds them all.
"""
import ast
import re

LIT = r"""('(?:[^'\\\n]|\\.)*'|"(?:[^"\\\n]|\\.)*")"""
CALL = re.compile(r"(?<![\w.$])_\(\s*" + LIT)
CALL_CTX = re.compile(r"(?<![\w.$])_p\(\s*" + LIT + r"\s*,\s*" + LIT)
COMMENT = re.compile(r"/\*.*?\*/|(?<![:'\"\\])//[^\n]*", re.S)


def _blank_comments(src):
    # Keep line numbers: replace comments with the same number of newlines
    return COMMENT.sub(lambda m: "\n" * m.group(0).count("\n"), src)


def extract_js(fileobj, keywords, comment_tags, options):
    src = fileobj.read().decode(options.get("encoding", "utf-8"))
    src = _blank_comments(src)
    for m in CALL.finditer(src):
        try:
            text = ast.literal_eval(m.group(1))
        except (ValueError, SyntaxError):
            continue
        lineno = src.count("\n", 0, m.start()) + 1
        yield lineno, "_", text, []
    for m in CALL_CTX.finditer(src):
        try:
            context, text = ast.literal_eval(m.group(1)), ast.literal_eval(m.group(2))
        except (ValueError, SyntaxError):
            continue
        lineno = src.count("\n", 0, m.start()) + 1
        yield lineno, "pgettext", (context, text), []
