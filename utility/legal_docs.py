"""Renders the canonical legal Markdown sources (licence.md, tos.md,
privacy.md) into HTML partials the site templates include.

Run once at server startup so the hosted pages always reflect whatever is
currently committed in the .md files, without needing a separate build step.
"""

import os

import markdown

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
PARTIALS_DIR = os.path.join(BASE_DIR, "templates", "legal")

# name -> source Markdown file at the repo root
DOCS = {
    "licence": "licence.md",
    "tos": "tos.md",
    "privacy": "privacy.md",
}

MD_EXTENSIONS = ["extra", "sane_lists", "nl2br"]


def sync_legal_pages():
    """Render each source .md file into templates/legal/<name>.html.

    Returns the number of documents successfully rendered.
    """
    os.makedirs(PARTIALS_DIR, exist_ok=True)

    rendered = 0
    for name, filename in DOCS.items():
        src_path = os.path.join(BASE_DIR, filename)
        if not os.path.exists(src_path):
            print(f"[WARNING] Legal document source missing: {src_path}")
            continue

        with open(src_path, "r", encoding="utf-8") as f:
            text = f.read()

        html = markdown.markdown(text, extensions=MD_EXTENSIONS)

        out_path = os.path.join(PARTIALS_DIR, f"{name}.html")
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(html)
        rendered += 1

    return rendered
