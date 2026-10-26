"""Cheap, no-AI pre-check for the needs-response backfill (2026-10-16): does this email contain ANY
free text a person could have written? Scanner jobs, empty bodies and no-reply senders can't be
'waiting on us', so they're settled without an AI call."""
import re

_AUTO_SENDER = re.compile(r"no-?reply|do-?not-?reply|donotreply|notifications?@|mailer-daemon|postmaster|"
                          r"alerts?@|automated|scanner|scan@", re.I)
_CUT = re.compile(r"(?im)^(-{2,}\s*forwarded message|-{2,}\s*original message|begin forwarded message|"
                  r"from:\s.+|on .{5,80} wrote:|sent from my )")


def human_text(body):
    """The top of the body, before any forwarded/quoted part -- what the latest sender typed."""
    m = _CUT.search(body or "")
    top = (body or "")[: m.start()] if m else (body or "")
    top = re.sub(r"\[cid:[^\]]*\]|<[^>]+>|https?://\S+", " ", top)
    return re.sub(r"\s+", " ", top).strip()


def worth_checking(from_addr, subject, body):
    """False = certainly no human request (skip the AI call)."""
    if _AUTO_SENDER.search(from_addr or ""):
        return False
    if re.match(r"(?i)\s*scan to e-?mail server job", subject or "") and len(human_text(body).split()) < 3:
        return False
    top = human_text(body)
    if len(top.split()) < 3:
        # nothing typed above a forward -- but a vendor's own (non-forward) email body is all "top"
        return False
    return True
