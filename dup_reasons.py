"""Short "why flagged" text for Possible duplicates (2026-10-20, Joel: the reasons were long and full of
file names). short(reason) maps the pipeline's fixed duplicate wordings to one short line; anything
else becomes its first sentence with file names and [tags] removed. The full text stays in a tooltip."""
import re

_FILE = re.compile(r"\s-\s[0-9a-f]{12,}\.(?:eml|msg)", re.I)   # the " - <gmail id>.eml" tail of a file name


def _amt(v):
    try:
        return f"${float(str(v).replace(',', '').replace('$', '')):,.2f}"
    except (TypeError, ValueError):
        return f"${v}"


def short(reason):
    t = re.sub(r"\s+", " ", (reason or "")).strip()
    if not t:
        return ""
    low = t.lower()
    m = re.search(r"looks like #(\S+?) from .*differ by one character", t, re.I)
    if m:
        return f"Looks like #{m.group(1)} with one character misread."
    m = re.search(r"already recorded to (.+?) from .+?this copy has it under (.+?)\.?$", t, re.I)
    if m:
        return f"Same bill already recorded under {m.group(1)}; this copy says {m.group(2)}."
    m = re.search(r"for \$([\d.,]+), but this copy shows \$([\d.,]+)", t, re.I)
    if m and "revised" in low:
        return f"Same #, different amount ({_amt(m.group(1))} vs {_amt(m.group(2))}) — maybe a revised bill."
    if "keeping this store-scanned copy" in low:
        return "Store scan kept; the vendor's earlier copy was set aside."
    if "vendor copy is auto-ignored" in low or "superseded by the store-scanned copy" in low:
        return "Store scan already recorded — this vendor copy set aside."
    if "second scan is auto-ignored" in low:
        return "Second scan of the same bill — set aside."
    if "[first-arrived]" in low or "duplicate copy, auto-ignored" in low:
        return "Same bill came in earlier — this copy set aside."
    m = re.search(r"^Same bill as #(\S+) \(record \d+\), already entered", t)
    if m:
        return f"Same bill as #{m.group(1)}, already in QB — # differs only by a tag."
    m = re.search(r"as #(\S+) dated [\d-]+(?:; #\S+ dated [\d-]+)*, already entered in QuickBooks", t)
    if m:
        return f"Same vendor, amount and store as #{m.group(1)} (already in QB), but a different #."
    if re.search(r"date, and store as .* DIFFERENT invoice number", t):
        return "Same vendor, amount, date and store — different #."
    if "one character apart" in low and "dates" in low:
        return "Same vendor, amount and store; # one character off, dates a few days apart."
    m = re.search(r"^No invoice # on these copies:.*dated ([\d, -]+?) --", t)
    if m:
        return f"No invoice #; same vendor, store and amount (dated {m.group(1)}) — likely scanned twice."
    m = re.search(r"^Same payment as", t)
    if m:
        return "Same payment as an earlier copy — the vendor's copy was kept."
    m = re.search(r"^Part of payment (\S+)", t)
    if m:
        return f"Part of payment {m.group(1)}, entered as one total."
    if low.startswith("a payment reminder for"):
        return "A payment reminder for a bill already recorded."
    first = re.split(r"(?<=[.!?])\s", t, 1)[0]
    first = _FILE.sub("", first)
    first = re.sub(r"\s*\[[a-z-]+\]", "", first).strip(" —-")
    return first if len(first) <= 120 else first[:117].rsplit(" ", 1)[0] + "…"
