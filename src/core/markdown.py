"""Markdown for long descriptions: rendered, then sanitised.

Two layers, because either alone is not enough:
1. markdown-it renders CommonMark with raw HTML switched off, so `<script>` in the source is
   shown as text rather than passed through.
2. nh3 (Rust's ammonia) then strips anything outside a small allow-list from the output, and
   forces links to http/https/mailto with rel="nofollow noopener noreferrer".

Images are not allowed: an <img> pointing at another host would break the offline rule (and the
CSP would block it anyway). Projects show pictures through their uploaded image gallery.
"""

import nh3
from django.utils.safestring import mark_safe
from markdown_it import MarkdownIt

_md = MarkdownIt("commonmark", {"html": False}).enable(["table", "strikethrough"])

ALLOWED_TAGS = {
    "p", "br", "hr", "h1", "h2", "h3", "h4", "h5", "h6", "strong", "em", "del", "code", "pre",
    "blockquote", "ul", "ol", "li", "a", "table", "thead", "tbody", "tr", "th", "td",
}
ALLOWED_ATTRIBUTES = {"a": {"href", "title"}, "ol": {"start"}}


def render(text):
    html = _md.render(text or "")
    return mark_safe(
        nh3.clean(
            html,
            tags=ALLOWED_TAGS,
            attributes=ALLOWED_ATTRIBUTES,
            url_schemes={"http", "https", "mailto"},
            link_rel="nofollow noopener noreferrer",
        )
    )


# Comments are user-generated content on a public page: a smaller allow-list (no headings, tables or
# rules), links marked ugc, and no images at all -- an image in a comment's Markdown is dropped by
# the renderer (see _comment_md) and would be stripped by nh3 anyway, so nothing ever asks another
# host for a picture (the CSP's img-src 'self' data: is a third layer).
COMMENT_TAGS = {"p", "br", "strong", "em", "del", "code", "pre", "blockquote", "ul", "ol", "li", "a"}
COMMENT_LINK_REL = "nofollow ugc noopener"

_comment_md = MarkdownIt("commonmark", {"html": False}).enable(["strikethrough"]).disable(["image"])


def render_comment(text):
    html = _comment_md.render(text or "")
    return mark_safe(
        nh3.clean(
            html,
            tags=COMMENT_TAGS,
            attributes={"a": {"href", "title"}, "ol": {"start"}},
            url_schemes={"http", "https", "mailto"},
            link_rel=COMMENT_LINK_REL,
        )
    )
