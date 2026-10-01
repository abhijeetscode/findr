# Spec: Strip Quoted Reply Text from Gmail Body Text

Status: **Draft — not yet agreed, not implemented.**
Owner: findr
Related: `specs/gmail-connector.md` (the `gmail_mime.py` this spec extends), `specs/gmail-thread-id.md` (the sibling spec that surfaced this — capturing `thread_id` groups a conversation together, but doesn't address that each reply's `body_text` re-quotes the whole conversation before it).

## 1. Purpose & scope

Email replies conventionally quote everything before them. Today, `gmail_mime.py`'s `_extract_body_text()` takes the message body as-is — so in a 5-message thread, the 5th reply's `body_text` contains the original message, every prior reply, *and* its own new text, all concatenated. This is a real BM25 relevance problem, independent of threading: every reply in a chain ends up matching almost anything the original matched, and quoted content inflates term-frequency statistics across the whole conversation.

This spec strips quoted content from `body_text` before it's stored/indexed, keeping only each message's own new content.

### In scope
- Detecting and removing quoted content from both the HTML and plain-text extraction paths in `gmail_mime.py`.
- Replacing `body_text` in place — the stripped version is the *only* version stored.

### Out of scope
- **Storing the full, unstripped body anywhere.** Not needed: the "open in Gmail" deep link (`_source_url` in `search_router.py`, already built) takes the user to the real message in Gmail's own UI, which already shows the full quoted thread natively. There is no reason to duplicate that content in Findr's own store just to preserve it.
- **Perfect detection.** This is a heuristic, not a guaranteed-correct parser — see §3 and §5 for known failure modes. Best-effort is the deliberate target, not 100% accuracy.
- Slack/Notion — Gmail only, consistent with the sibling spec's scoping. (Both connectors since removed — see `specs/remove-slack-notion.md`.)
- Any change to `thread_id` capture — orthogonal, handled by `specs/gmail-thread-id.md`.

## 2. Detection method

Two independent heuristics, one per body type already handled by `_extract_body_text()`:

**HTML part — look for Gmail's own quote marker.** When Gmail composes a reply, it wraps the quoted content in `<div class="gmail_quote">...</div>` in the HTML part. This is a reliable, Gmail-specific signal — not a guess about formatting, an actual convention Gmail's own compose UI applies consistently. Strip everything from that div onward before tag-stripping.

**Plain-text part — look for a quote delimiter line.** No equivalent structural marker exists in plain text, so this falls back to matching known delimiter conventions and truncating the body at the first match:
- `On <date>, <sender> wrote:` (Gmail's own plain-text delimiter, and the most common one across clients generally)
- `-----Original Message-----` / `--------Original Message--------` (Outlook-style)
- A line starting with `>` (traditional quoted-line prefix, used by many clients including Gmail's plain-text export in some cases)

Whichever of these appears **first** in the text marks the start of quoted content — everything from there to the end is discarded, regardless of how many nested quote levels it contains (a reply-to-a-reply-to-a-reply all quoted in one message is discarded as one block, which is correct — only the current message's own new text should survive).

## 3. Implementation

`gmail_mime.py` changes — two new stripping functions, applied inside the existing `_extract_body_text()` before/around its current tag-stripping:

```python
_QUOTE_DELIMITER_RE = re.compile(
    r"^\s*On .+ wrote:\s*$"
    r"|^\s*-{2,}\s*Original Message\s*-{2,}\s*$"
    r"|^>",
    re.IGNORECASE | re.MULTILINE,
)

_GMAIL_QUOTE_HTML_RE = re.compile(r'<div class="gmail_quote".*', re.IGNORECASE | re.DOTALL)


def _strip_quoted_plain(text: str) -> str:
    match = _QUOTE_DELIMITER_RE.search(text)
    return text[: match.start()].rstrip() if match else text


def _strip_quoted_html(html: str) -> str:
    match = _GMAIL_QUOTE_HTML_RE.search(html)
    return html[: match.start()] if match else html


def _extract_body_text(payload: dict[str, Any]) -> str:
    plain = _find_part_body(payload, "text/plain")
    if plain is not None:
        return _strip_quoted_plain(plain)
    html = _find_part_body(payload, "text/html")
    if html is not None:
        return _strip_html(_strip_quoted_html(html))
    return ""
```

Deliberately regex-based, not a real HTML parser — consistent with `_strip_html`'s existing approach (`_TAG_RE = re.compile(r"<[^>]+>")`) in the same file. No new dependency for this.

## 4. Why in-place replacement, not dual storage

Considered and rejected: storing both a stripped and an unstripped copy. Rejected because nothing needs the unstripped version — the "open in Gmail" click-through already provides access to the full original content in Gmail's own UI, which is a strictly better reading experience for the full quoted thread than anything Findr would render itself. Storing both would mean indexing (and paying the storage/relevance cost of) the exact content this spec exists to remove.

## 5. Edge cases

| Edge case | Handling |
|---|---|
| First message in a thread (nothing to quote) | No delimiter/marker found — `body_text` is unchanged, no-op. |
| Email from a non-Gmail client (Outlook, Apple Mail, etc.) with no `gmail_quote` HTML marker | Falls through to whichever heuristic applies — HTML without the marker is left as-is (tag-stripped only); plain text still gets the delimiter-line check, which is more client-agnostic. |
| A genuinely new message that happens to contain a line starting with `>` or the literal phrase "wrote:" | A real but accepted false-positive risk of a heuristic approach — some legitimate content could be truncated. Judged an acceptable trade-off given the alternative (every reply duplicating the whole thread) is a worse, more common problem. |
| Quoted content that itself contains further nested quotes | Handled correctly by construction — truncating at the *first* delimiter discards everything after it as one block, regardless of internal nesting. |
| No plain-text *or* HTML part at all | Unchanged from today — `_extract_body_text` already returns `""` in this case. |

## 6. Verification

**Automated**: extends the existing `tests/adapters/test_gmail_mime.py` fixtures (none of which currently contain quoted content, confirmed — this change doesn't affect any existing test). New tests:
- A plain-text body containing `"On Tue, ... wrote:"` followed by quoted history — asserts only the text before the delimiter survives.
- A plain-text body with `>`-prefixed quoted lines — same assertion.
- An HTML body containing a `<div class="gmail_quote">` — asserts the quoted div's content doesn't appear in the final `body_text`.
- A plain first-message body with no quote markers — asserts it's unchanged (no false-positive truncation on ordinary content).

**Manual end-to-end**: sync a real Gmail thread with several replies, check the `body_text` stored in Postgres for a later reply, confirm it contains only that reply's own new text — not the whole quoted history.
