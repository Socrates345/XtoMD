"""Web pages for readlater: any saved link that is not an x.com post, read with Defuddle and cut down to the text
the model reads.

Defuddle is the extractor inside Obsidian Web Clipper: it finds a page's main content and leaves out the menus,
sidebars, ads and comments. It is a Node package, pinned in package.json next to this file; `npm install` in
readlater puts it in readlater/node_modules (README.md, Setup). It is run with node itself, never through npx or a
.cmd file: on Windows those go through cmd.exe, which would take the `&` of a link's query for a new command.

Defuddle fetches the page itself (10 s, 5 MB at most, HTML only), so it knows the link and can use what it has for
a few sites (Substack, Wikipedia, GitHub...).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFUDDLE_CLI = HERE / "node_modules" / "defuddle" / "dist" / "cli.js"
DEFUDDLE_TIMEOUT = 60.0  # seconds per page: Defuddle's own fetch gives up after 10, the rest is node and the parsing
MIN_PAGE_WORDS = 100  # fewer is no article: a page built by JavaScript, a login or a bot wall (README.md, Limits)
SETUP = "readlater/README.md, Setup"

_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
# what is left of a page's markup in Defuddle's markdown: whole blocks (an <svg> chart, seen live), then any tag
_BLOCK = re.compile(r"(?is)<(svg|script|style|noscript|iframe|object|embed|template|math)\b.*?</\1\s*>")
_TAG = re.compile(r"</?[a-zA-Z][^<>]*>")
_IMAGE = re.compile(r"!\[[^\]\n]*\]\([^)\n]*\)")
_LINK = re.compile(r"\[([^\]\n]*)\]\((?:[^()\s]|\([^()\s]*\))*(?:\s+\"[^\"\n]*\")?\)")
# code is left as it is: an article about HTML or about code says what it says with it
_CODE = re.compile(r"(?ms)^[ \t]*(`{3,}|~{3,}).*?^[ \t]*\1[ \t]*$|`[^`\n]+`")


class PageError(RuntimeError):
    """Why a page could not be read, in a few words."""


@dataclass(frozen=True)
class Extracted:
    """A page as Defuddle found it."""

    title: str
    author: str
    site: str
    published: str  # YYYY-MM-DD, or ""
    markdown: str  # the main content, as Defuddle writes it


def missing(node: str | None = None, cli: Path = DEFUDDLE_CLI) -> str:
    """What is missing to read web pages, or "" when nothing is."""
    node = shutil.which("node") if node is None else node
    if not node:
        return f"web links need Node.js: install its LTS from nodejs.org, then run `npm install` in readlater ({SETUP})"
    if Path(node).suffix.lower() in (".cmd", ".bat"):  # a version manager's shim: Windows runs it through cmd.exe
        return (f"node on PATH is a {Path(node).suffix} script ({node}), which Windows runs through cmd.exe, where the "
                "& of a link would start another command: put the node.exe of nodejs.org's installer first on PATH")
    if not cli.is_file():
        return f"web links need Defuddle: run `npm install` in readlater ({SETUP})"
    return ""


def reader(node: str | None = None, cli: Path = DEFUDDLE_CLI, run=subprocess.run):
    """`read(url) -> Extracted`, with the node on PATH unless one is named."""
    node = node or shutil.which("node") or "node"
    return lambda url: defuddle(url, node, cli, run)


def defuddle(url: str, node: str, cli: Path = DEFUDDLE_CLI, run=subprocess.run,
             timeout: float = DEFUDDLE_TIMEOUT) -> Extracted:
    """One page through Defuddle's command line. Raises PageError with the reason it could not be read."""
    try:
        done = run([node, str(cli), "parse", url, "--json", "--markdown"], stdin=subprocess.DEVNULL,
                   capture_output=True, encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        raise PageError(f"no answer in {timeout:.0f} s") from None
    except OSError as exc:
        raise PageError(f"node could not be run: {exc.strerror or exc}") from None
    if done.returncode != 0:
        raise PageError(failure(done.stderr or ""))
    try:
        data = json.loads(done.stdout or "")
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise PageError("Defuddle's answer could not be read")
    content = data.get("contentMarkdown") or data.get("content")
    published = _DATE.match(_text(data.get("published")))
    return Extracted(_text(data.get("title")), _text(data.get("author")), _text(data.get("site")),
                     published.group(0) if published else "", content if isinstance(content, str) else "")


def failure(stderr: str) -> str:
    """Defuddle's error in a few words, and what to make of it."""
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    message = lines[-1] if lines else "Defuddle failed without saying why"
    message = re.sub(r"^Error:\s*", "", message)[:200]
    if message.startswith("No content could be extracted"):
        return "no text found: the page may build it with JavaScript"
    if message == "fetch failed":
        return "the site could not be reached (no connection, or a wrong address)"
    if re.search(r"\b(?:401|403)\b", message):
        return f"{message}: the site turns scripts away (Medium does), or needs a login"
    if re.search(r"\b429\b", message):
        return f"{message}: the site asks to wait, run it again later"
    return message


def clean_page(markdown: str) -> str:
    """Defuddle's markdown as the model reads it: markup left over from the page, pictures and the addresses of
    links taken out (a link keeps its words), spaces collapsed, at most one blank line in a row. Code stays as it is.
    Seen live: an Astro page's <svg> chart (14,000 characters) and a Substack essay's 370 links."""
    def tidy(part: str) -> str:
        part = _TAG.sub(" ", _BLOCK.sub(" ", part))
        part = _LINK.sub(r"\1", _IMAGE.sub("", part))
        # spaces at the ends of lines only: a part may start or end next to inline code, where its space counts
        return re.sub(r"(?<=\n) | (?=\n)", "", re.sub(r"[ \t ]+", " ", part))

    out, last = [], 0
    for match in _CODE.finditer(markdown):
        out += [tidy(markdown[last : match.start()]), match.group(0)]
        last = match.end()
    out.append(tidy(markdown[last:]))
    return re.sub(r"\n{3,}", "\n\n", "".join(out)).strip()


def image_count(markdown: str) -> int:
    return len(_IMAGE.findall(markdown))


def _text(value: object) -> str:
    return " ".join(value.split()) if isinstance(value, str) else ""
