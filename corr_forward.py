"""Forward correspondence emails (2026-10-15).

forward_email(): sends a stored correspondence email on to one or more people through the AP Gmail
mailbox the pipeline already reads (google_auth -- gmail.modify includes send, so no new consent).
The forward is a normal "Fwd:" email: your note, the original's From/Date/Subject/To header block,
the original text, and the original attachments re-attached. Source = the stored .eml/.msg in
processed_emails/ (or review_pending/ / test_emails/); if there's no local copy, the raw message is
pulled from Gmail by its message id.

run_forward_rules(): the automatic side -- for every OPEN correspondence email that matches a
'forward' rule and arrived after the rule was created (database.forward_rule_matches), forward it
and mark it Handled. Called at the end of each classify run.
"""
import base64
import email
import email.policy
import re
from email.message import EmailMessage
from pathlib import Path

import database

BASE_DIR = Path(__file__).parent
_DIRS = ("processed_emails", "review_pending", "test_emails")
_ADDR = re.compile(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+")


def parse_recipients(text):
    """'a@x.com, Bob <b@y.com>' -> ['a@x.com', 'b@y.com'] (deduped, lowercased). [] if none valid."""
    out = []
    for a in _ADDR.findall(text or ""):
        a = a.lower()
        if a not in out:
            out.append(a)
    return out


def _gmail():
    import google_auth
    return google_auth.build_service("gmail", "v1")


def _load_original(filename, gmail_message_id=None):
    """(headers dict, body text, [(name, bytes, maintype, subtype)]) of the original email."""
    path = next((BASE_DIR / d / filename for d in _DIRS if (BASE_DIR / d / filename).is_file()), None)
    if path is not None and path.suffix.lower() == ".msg":
        import extract_msg
        msg = extract_msg.Message(str(path))
        try:
            hb = msg.htmlBody or ""
            if isinstance(hb, bytes):
                hb = hb.decode("utf-8", "ignore")
            body = msg.body or _html_to_text(hb)
            atts = []
            for a in msg.attachments:
                data = a.data if isinstance(a.data, bytes) else None
                if data:
                    atts.append((a.longFilename or a.shortFilename or "attachment", data,
                                 "application", "octet-stream"))
            hdr = {"From": msg.sender or "", "Date": str(msg.date or ""), "Subject": msg.subject or "",
                   "To": msg.to or ""}
            return hdr, body, atts
        finally:
            msg.close()
    if path is not None and path.suffix.lower() == ".eml":
        raw = path.read_bytes()
    elif gmail_message_id:
        res = _gmail().users().messages().get(userId="me", id=gmail_message_id, format="raw").execute()
        raw = base64.urlsafe_b64decode(res["raw"])
    elif path is not None:                       # .txt
        return {"Subject": path.stem}, path.read_text(encoding="utf-8", errors="replace"), []
    else:
        raise ValueError("the original email isn't stored locally and has no Gmail id")
    m = email.message_from_bytes(raw, policy=email.policy.default)
    plain, html_part, atts = None, None, []
    for part in m.walk():
        if part.is_multipart():
            continue
        fn = part.get_filename()
        if fn or part.get_content_disposition() == "attachment":
            data = part.get_payload(decode=True) or b""
            mt, _, st = (part.get_content_type() or "application/octet-stream").partition("/")
            atts.append((fn or "attachment", data, mt or "application", st or "octet-stream"))
            continue
        try:
            content = part.get_content()
        except Exception:
            continue
        if part.get_content_type() == "text/plain" and plain is None:
            plain = content
        elif part.get_content_type() == "text/html" and html_part is None:
            html_part = content
    body = plain if (plain and plain.strip()) else _html_to_text(html_part or "")
    hdr = {k: str(m.get(k) or "") for k in ("From", "Date", "Subject", "To", "Cc")}
    return hdr, body, atts


def _html_to_text(h):
    import html as _html
    h = re.sub(r"(?is)<(script|style).*?</\1>", " ", h or "")
    h = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</li>", "\n", h)
    h = re.sub(r"<[^>]+>", " ", h)
    return re.sub(r"\n{3,}", "\n\n", _html.unescape(h)).strip()


def build_forward(filename, to_addrs, note="", gmail_message_id=None):
    hdr, body, atts = _load_original(filename, gmail_message_id)
    subj = hdr.get("Subject") or filename
    msg = EmailMessage()
    msg["To"] = ", ".join(to_addrs)
    msg["Subject"] = subj if re.match(r"(?i)^(fwd?|fw):", subj) else f"Fwd: {subj}"
    lines = []
    if (note or "").strip():
        lines += [note.strip(), ""]
    lines += ["---------- Forwarded message ---------"]
    for k in ("From", "Date", "Subject", "To", "Cc"):
        if hdr.get(k):
            lines.append(f"{k}: {hdr[k]}")
    lines += ["", body or "(no text in the original email)"]
    msg.set_content("\n".join(lines))
    for name, data, mt, st in atts:
        msg.add_attachment(data, maintype=mt, subtype=st, filename=name)
    return msg


def forward_email(filename, to_text, note, username, mark_handled=False):
    """Forward one stored email to the address(es) in to_text. Returns the recipient list.
    Records who/when it went to (emails.corr_forwarded_to accumulates across forwards) but leaves the
    email's status alone -- a person decides separately whether it's handled (Joel, 2026-10-15).
    mark_handled=True is only used by automatic forward rules, which have no person to decide.
    Raises ValueError on bad input / missing source; Gmail errors propagate."""
    to_addrs = parse_recipients(to_text)
    if not to_addrs:
        raise ValueError("enter at least one valid email address to forward to")
    conn = database.connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT gmail_message_id FROM emails WHERE filename = %s", (filename,))
        row = cur.fetchone()
        gid = row[0] if row else None
        msg = build_forward(filename, to_addrs, note, gid)
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        _gmail().users().messages().send(userId="me", body={"raw": raw}).execute()
        who = ", ".join(to_addrs)
        cur.execute("UPDATE emails SET corr_forwarded_to = CASE WHEN coalesce(corr_forwarded_to,'') = '' THEN %s "
                    "ELSE corr_forwarded_to || '; ' || %s END, corr_forwarded_at = now() WHERE filename = %s",
                    (who, who, filename))
        if mark_handled:
            full_note = f"Forwarded to {who}" + (f" — {note.strip()}" if (note or "").strip() else "")
            cur.execute("UPDATE emails SET corr_status = 'handled', corr_note = %s, corr_by = %s, corr_at = now() "
                        "WHERE filename = %s", (full_note, username, filename))
        conn.commit()
        try:
            cur.execute("INSERT INTO edit_log (table_name, record_id, field, old_value, new_value, changed_by) "
                        "VALUES ('emails', NULL, %s, NULL, %s, %s)",
                        (f"forwarded ({filename})"[:200], who + (f" — {note.strip()}" if (note or "").strip() else ""),
                         username))
            conn.commit()
        except Exception:
            conn.rollback()
        return to_addrs
    finally:
        conn.close()


def read_recipients(filename):
    """'a@x.com, b@y.com' -- the To + Cc addresses of a stored email (headers only, fast), lowercased.
    '' when the file has none / isn't stored here."""
    path = next((BASE_DIR / d / filename for d in _DIRS if (BASE_DIR / d / filename).is_file()), None)
    if path is None:
        return ""
    try:
        if path.suffix.lower() == ".eml":
            from email.parser import BytesHeaderParser
            with open(path, "rb") as f:
                h = BytesHeaderParser(policy=email.policy.compat32).parse(f)
            text = " ".join(str(h.get(k) or "") for k in ("To", "Cc"))
        elif path.suffix.lower() == ".msg":
            import extract_msg
            m = extract_msg.Message(str(path))
            try:
                text = f"{m.to or ''} {m.cc or ''}"
            finally:
                m.close()
        else:
            return ""
    except Exception:
        return ""
    return ", ".join(parse_recipients(text))


def fill_recipients(cur):
    """Store To+Cc on correspondence emails that don't have it yet (so To/CC rules can match).
    Uses the caller's cursor; returns how many it filled."""
    cur.execute("SELECT filename FROM emails WHERE (email_type = 'correspondence' OR needs_response IS TRUE) "
                "AND recipients IS NULL")
    n = 0
    for (fn,) in cur.fetchall():
        cur.execute("UPDATE emails SET recipients = %s WHERE filename = %s", (read_recipients(fn), fn))
        n += 1
    return n


def run_forward_rules(username="classify run"):
    """Forward every pending match of a 'forward' rule. Returns (sent, failed). Never raises."""
    sent, failed = 0, 0
    try:
        matches = database.forward_rule_matches()
    except Exception as e:
        print(f"(forward rules: could not read matches: {e})")
        return 0, 0
    for filename, rule_id, forward_to, note in matches:
        try:
            forward_email(filename, forward_to, note or "", f"{username} (rule #{rule_id})", mark_handled=True)
            sent += 1
        except Exception as e:
            failed += 1
            print(f"(forward rule #{rule_id} failed for {filename}: {e})")
    return sent, failed
