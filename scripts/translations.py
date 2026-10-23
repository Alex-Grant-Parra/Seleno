"""Manage the translation files.

    python scripts/translations.py update    # collect marked text, refresh every .po
    python scripts/translations.py compile   # check placeholders, build .mo files (the server also builds them on start)
    python scripts/translations.py status    # how much of each language is done
    python scripts/translations.py add fr    # start a new language

Only edit translations/<lang>/LC_MESSAGES/messages.po. messages.pot and the
.mo files are generated.
"""
import os
import re
import subprocess
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
TRANSLATIONS = os.path.join(ROOT, "translations")
POT = os.path.join(TRANSLATIONS, "messages.pot")


def pybabel(*args):
    subprocess.run([sys.executable, "-m", "babel.messages.frontend", *args], cwd=ROOT, check=True)


def languages():
    if not os.path.isdir(TRANSLATIONS):
        return []
    return sorted(d for d in os.listdir(TRANSLATIONS)
                  if os.path.isfile(os.path.join(TRANSLATIONS, d, "LC_MESSAGES", "messages.po")))


def extract():
    os.makedirs(TRANSLATIONS, exist_ok=True)
    pybabel("extract", "-F", "babel.cfg", "-k", "_l", "-k", "lazy_gettext",
            "--sort-by-file", "--no-wrap", "--project", "SELENO",
            "-o", os.path.relpath(POT, ROOT), ".")


def po_path(lang):
    return os.path.join(TRANSLATIONS, lang, "LC_MESSAGES", "messages.po")


def fix_js_flags(path):
    """JavaScript text isn't a Python format string, whatever "% of" looks like.

    Babel (and Poedit, via msgfmt) check entries flagged python-format, so text
    such as "{pct}% of the sky" would fail. Entries used only in static/js get
    no-python-format instead.
    """
    blocks = open(path, encoding="utf-8").read().split("\n\n")
    out = []
    for block in blocks:
        lines = block.split("\n")
        refs = [r for l in lines if l.startswith("#: ") for r in l[3:].split()]
        if refs and all(r.startswith("static/js/") for r in refs):
            for i, l in enumerate(lines):
                if l.startswith("#, ") and "python-format" in l:
                    flags = [f.strip() for f in l[3:].split(",")]
                    flags = [f for f in flags if f != "python-format"]
                    if "no-python-format" not in flags:
                        flags.append("no-python-format")
                    lines[i] = "#, " + ", ".join(flags)
        out.append("\n".join(lines))
    open(path, "w", encoding="utf-8").write("\n\n".join(out))


def update():
    extract()
    for lang in languages():
        pybabel("update", "-i", POT, "-d", TRANSLATIONS, "-l", lang, "--no-wrap")
        fix_js_flags(po_path(lang))
    status()


PLACEHOLDERS = re.compile(r"\{\w+\}|%\(\w+\)s|</?[a-z][^>]*>")


def check(lang):
    """Translations must keep every placeholder and HTML tag of the English."""
    from babel.messages.pofile import read_po
    with open(po_path(lang), "rb") as f:
        cat = read_po(f, locale=lang)
    problems = []
    for m in cat:
        if not m.id or not m.string or m.fuzzy or isinstance(m.id, tuple):
            continue
        if sorted(PLACEHOLDERS.findall(m.id)) != sorted(PLACEHOLDERS.findall(m.string)):
            problems.append((m.lineno, m.id, m.string))
    for lineno, msgid, msgstr in problems:
        print(f"{lang} line {lineno}: placeholders differ\n  en: {msgid}\n  {lang}: {msgstr}")
    return not problems


def compile_all():
    from babel.messages.mofile import write_mo
    from babel.messages.pofile import read_po
    ok = True
    for lang in languages():
        ok = check(lang) and ok
        with open(po_path(lang), "rb") as f:
            cat = read_po(f, locale=lang)
        with open(po_path(lang)[:-3] + ".mo", "wb") as f:
            write_mo(f, cat)
        print(f"compiled {lang}")
    if not ok:
        sys.exit(1)


def add(lang):
    extract()
    pybabel("init", "-i", POT, "-d", TRANSLATIONS, "-l", lang, "--no-wrap")
    print(f"Created translations/{lang}/LC_MESSAGES/messages.po - also add it to LANGUAGES in app/i18n.py")


def status():
    from babel.messages.pofile import read_po
    for lang in languages():
        with open(os.path.join(TRANSLATIONS, lang, "LC_MESSAGES", "messages.po"), "rb") as f:
            cat = read_po(f, locale=lang)
        msgs = [m for m in cat if m.id]
        done = sum(1 for m in msgs if m.string and not m.fuzzy and all(m.string if isinstance(m.string, tuple) else [m.string]))
        fuzzy = sum(1 for m in msgs if m.fuzzy)
        print(f"{lang}: {done}/{len(msgs)} translated, {fuzzy} need review (fuzzy), {len(msgs) - done - fuzzy} empty")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "update":
        update()
    elif cmd == "compile":
        compile_all()
    elif cmd == "status":
        status()
    elif cmd == "add" and len(sys.argv) == 3:
        add(sys.argv[2])
    else:
        print(__doc__)
