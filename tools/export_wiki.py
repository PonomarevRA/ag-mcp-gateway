"""Stage the public bilingual Markdown as a separate GitHub Wiki checkout payload."""

import argparse
from pathlib import Path
import re

from build_standalone import PUBLIC_WIKI, ROOT

_SOURCE_LINK = re.compile(r"\]\(\.\./\.\./\.\./(profiles/[a-z0-9.-]+\.json)\)")
_PUBLIC_BLOB = "https://github.com/PonomarevRA/ag-mcp-gateway/blob/main/"


def export(destination: Path) -> None:
    """Write only approved wiki pages, with GitHub Wiki slugs and public profile URLs."""
    wiki = ROOT / "docs" / "public" / "wiki"
    documents = {}
    for name in PUBLIC_WIKI:
        source = wiki / name
        if source.is_symlink():
            raise ValueError(f"Wiki source is a symlink: {name}")
        contents = source.read_text(encoding="utf-8")
        for link in PUBLIC_WIKI:
            contents = contents.replace(f"]({link})", f"]({Path(link).stem})")
        documents[name] = _SOURCE_LINK.sub(lambda match: f"]({_PUBLIC_BLOB}{match[1]})", contents)
    destination.mkdir()
    for name, contents in documents.items():
        (destination / name).write_text(contents, encoding="utf-8")


def main() -> None:
    """Prepare the pages in a new directory without touching a remote repository."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    export(args.destination)
    print(f"Prepared {len(PUBLIC_WIKI)} Wiki pages in {args.destination}")


if __name__ == "__main__":
    main()
