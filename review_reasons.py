"""Short, scannable reasons for the Review queue (2026-10-20, Joel).

The queue's "Why flagged" text is a run-on mix of fixed sentences the pipeline appends ("No invoice date
found -- needs a human...", "No standardized vendor mapping found for 'X'...") and the AI's own wording.
summarize() turns it into colored reason chips + one short leftover line, with repeated sentences
removed. Pure text matching -- no AI call, works on items already in the queue."""
import re

# key -> (label, css class). Order = display order.
REASONS = [
    ("dup",     "Possible duplicate",   "rr-dup"),
    ("vendor",  "Vendor not in list",   "rr-vendor"),
    ("account", "No QB account",        "rr-vendor"),
    ("store",   "Store unknown",        "rr-miss"),
    ("date",    "Date missing",         "rr-miss"),
    ("amount",  "Amount missing",       "rr-miss"),
    ("total",   "Total doesn't add up", "rr-miss"),
    ("page",    "Page missing",         "rr-miss"),
    ("attach",  "Attachment missing",   "rr-miss"),
    ("read",    "Hard to read",         "rr-read"),
    ("unknown", "Unknown document",     "rr-read"),
]
LABELS = {k: (lbl, css) for k, lbl, css in REASONS}

# (key, pattern, sentence_is_fully_covered). A fully-covered sentence is the pipeline's own fixed
# wording -- the chip says it all, so the sentence isn't repeated in the leftover line.
_RULES = [
    ("dup", r"multiple versions of invoice", True),
    ("dup", r"possible duplicate|already (been )?(recorded|entered)|same invoice (was|as)|duplicate of", False),
    ("vendor", r"no standardized vendor mapping found", True),
    ("vendor", r"^add it to the vendor names reference list", True),
    ("account", r"has no quickbooks account assigned", True),
    ("account", r"^add it to the vendor accounts reference list", True),
    ("store", r"store could not be identified", True),
    ("store", r"no identifiable store|store (is )?(unknown|unclear|ambiguous)|which store|store conflict|conflicting store", False),
    ("date", r"^no [a-z ]*date found", True),
    ("date", r"no (invoice )?date|date (is )?(missing|not shown|not found|illegible|unclear)", False),
    ("amount", r"^no usable amount was found", True),
    ("amount", r"amount (is |was )?(not shown|missing|not (visible|found|included)|unknown|must be confirmed|cannot be verified|can't be verified)"
               r"|no (invoice |total )?amount|invoice_amount cannot|total (is )?(missing|not shown)", False),
    ("total", r"does not reconcile|doesn't reconcile|do(es)?n'?t (add up|match the (sum|total))|don't add up|"
              r"total .{0,40}(does not|doesn't) match|line items? .{0,30}(sum|add)", False),
    ("page", r"page \d+ of|page: ?\d+ of|second page|missing page|page \d+.{0,30}missing|'continued'|\bcontinued\b", False),
    ("attach", r"attachment .{0,100}(not included|missing|not attached|wasn't included|not readable)|no attachment|"
               r"not attached|portal sign-?in|behind a (portal|login)", False),
    ("read", r"illegible|unreadable|upside[- ]down|blurr|hard to read|cannot be read|can't be read|couldn't read|"
             r"could not read|not legible|faded|cut off", False),
    ("unknown", r"couldn't tell what it is|could not tell what it is|unclear what (this|the) document", False),
]
_RULES = [(k, re.compile(p, re.I), full) for k, p, full in _RULES]
_VENDOR_NAME = re.compile(r"no standardized vendor mapping found for '(.*?)'\. Add it", re.I)
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z'\"(])")


def _sentences(text):
    out, seen = [], set()
    for chunk in re.split(r"\n+", text or ""):
        for s in _SENT_SPLIT.split(chunk.strip()):
            s = s.strip()
            k = re.sub(r"\W+", " ", s.lower()).strip()
            if s and k not in seen:          # repeated sentences shown once
                seen.add(k)
                out.append(s)
    return out


def summarize(question, duplicate_group_id=None, max_rest=130):
    """{"keys": [...], "chips": [(key, label, css)], "rest": short leftover text, "full": deduped text,
    "vendor_name": the unmapped vendor spelling or None}."""
    sents = _sentences(question)
    keys, rest = [], []
    for s in sents:
        covered = False
        for k, rx, full in _RULES:
            if rx.search(s):
                if k not in keys:
                    keys.append(k)
                covered = covered or full
        if not covered:
            rest.append(s)
    if duplicate_group_id and "dup" not in keys:
        keys.append("dup")
    m = _VENDOR_NAME.search(question or "")
    order = [k for k, _l, _c in REASONS]
    keys.sort(key=order.index)
    short = " ".join(rest)
    if len(short) > max_rest:
        short = short[:max_rest].rsplit(" ", 1)[0].rstrip(",;:—-") + "…"
    return {"reasons": keys, "chips": [(k,) + LABELS[k] for k in keys], "rest": short,
            "rest_full": " ".join(rest), "full": "\n".join(sents),
            "vendor_name": (m.group(1) if m and len(m.group(1)) <= 60 and "illegible" not in m.group(1).lower()
                            else None)}
