"""Put a saved PDF's pages in the right order -- same Drive link, so the record keeps pointing at it.

  python fix_page_order.py "<Drive link or file name>" 2,1
      -> the new file is page 2 then page 1 (any order of all the page numbers works, e.g. 3,1,2)

Updates the Drive copy in place (the link doesn't change) and the local copy in invoice_files /
payment_files if there is one. The old version is kept in _tmp_view/page_order_backups/."""
import io
import re
import sys
import shutil
from datetime import datetime
from pathlib import Path

from pypdf import PdfReader, PdfWriter

ROOT = Path(__file__).parent
LOCAL_DIRS = ("invoice_files", "payment_files", "review_pending")


def main():
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(1)
    target, order_txt = sys.argv[1].strip(), sys.argv[2].strip()
    order = [int(x) for x in re.split(r"[,\s]+", order_txt) if x]

    from drive_uploader import DriveUploader, NUM_RETRIES
    svc = DriveUploader().service
    m = re.search(r"/d/([A-Za-z0-9_-]{20,})", target) or re.search(r"[?&]id=([A-Za-z0-9_-]{20,})", target)
    file_id, name = None, target
    if m:
        file_id = m.group(1)
        name = svc.files().get(fileId=file_id, fields="name").execute(num_retries=NUM_RETRIES)["name"]
    local = next((ROOT / d / name for d in LOCAL_DIRS if (ROOT / d / name).is_file()), None)
    if local:
        data = local.read_bytes()
    elif file_id:
        data = svc.files().get_media(fileId=file_id).execute(num_retries=NUM_RETRIES)
    else:
        print(f"Can't find '{name}' locally -- pass the Drive link instead.")
        sys.exit(1)

    reader = PdfReader(io.BytesIO(data))
    n = len(reader.pages)
    if sorted(order) != list(range(1, n + 1)):
        print(f"'{name}' has {n} page(s); the order must list each of 1..{n} once (got {order}).")
        sys.exit(1)
    writer = PdfWriter()
    for p in order:
        writer.add_page(reader.pages[p - 1])
    buf = io.BytesIO()
    writer.write(buf)
    new = buf.getvalue()

    bdir = ROOT / "_tmp_view" / "page_order_backups"
    bdir.mkdir(parents=True, exist_ok=True)
    (bdir / f"{datetime.now():%Y%m%d_%H%M%S} {name}").write_bytes(data)
    if local:
        local.write_bytes(new)
        print(f"Local copy reordered: {local.relative_to(ROOT)}")
    if file_id:
        from googleapiclient.http import MediaIoBaseUpload
        svc.files().update(fileId=file_id, media_body=MediaIoBaseUpload(io.BytesIO(new), mimetype="application/pdf")
                           ).execute(num_retries=NUM_RETRIES)
        print("Drive copy reordered (same link).")
    print(f"Done: pages now {order}. Old version saved in _tmp_view/page_order_backups/.")


if __name__ == "__main__":
    main()
