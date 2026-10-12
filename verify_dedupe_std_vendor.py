"""READ-ONLY. Verifies the 2026-10-08 duplicate-detection change (classify_emails._dedupe_vendor_key)
against the REAL vendor table: every pair the cleanup found must now resolve to the same vendor key
and check_duplicate must call it 'duplicate'; unrelated vendors must stay apart. Writes nothing."""
import os
os.environ.setdefault("STATEMENT_GOLIVE_DATE", "2026-08-07")
import classify_emails as ce

ce.VENDOR_STD = ce.load_vendor_standardization()
pairs = [("Costco (Same-Day/Instacart)", "Costco"), ("MaxiHealth", "Maxi Health"), ("Maxi-Health", "Maxi Health"),
         ("Prestige Sales Inc.", "A&S Sales"), ("Prestige Sales Inc. (A&S Sales)", "Prestige Sales Inc"),
         ("Avanos Medical Sales LLC", "Avanos"), ("B & C Industries", "B&C Industries"),
         ("Braun HVAC & Appliance Repair Corp", "Braun"), ("Crown Care Services", "Crown Care"),
         ("American Health (Garden of Life)", "Garden of Life"), ("Greenstar", "GreenStar Naturals"),
         ("Incanto Magic II LLC", "Incento Magic II LLC"), ("Kenover Marketing Corp", "KayCo"),
         ("Megafood (Foodstate, Inc.)", "Megafood"), ("Nutri-Supreme Research", "Nutri-Supreme"),
         ("Paskesz Candy Co., Inc.", "Paskesz"), ("Prodemix", "Prodermix"), ("Wellspring Meds", "Wellspring"),
         ("Advanced Nutrition by Zahler Inc.", "Zahler"), ("Marine Ark Distribution", "Marine Park Distribution"),
         ("Schwartz & Company CPA's PLLC", "Schwartz & Company CPAs PLLC"), ("Smilling Herbs Inc", "Smiling Herbs Inc")]
negatives = [("McKesson", "Parmed"), ("Garden of Life", "Zahler"), ("Costco", "SmartSource"), ("Braun", "Crown Care"),
             ("Paskesz", "KayCo"), ("A&S Sales", "Avanos")]
ok = True
print("Should MATCH:")
for a, b in pairs:
    ka, kb = ce._dedupe_vendor_key(a), ce._dedupe_vendor_key(b)
    hist = [{"vendor_matched": a, "invoice_number": "X1", "invoice_amount": 10.0, "source_file": "a", "source_email_id": "a"}]
    st = ce.check_duplicate({"vendor_matched": b, "invoice_number": "X1", "invoice_amount": 10.0}, hist, source_id="b", source_file="b")[0]
    good = ka == kb and st == "duplicate"
    ok &= good
    print(f"  {'OK  ' if good else 'FAIL'} {a!r} -> {ka!r}  |  {b!r} -> {kb!r}  check_duplicate={st}")
print("Should stay APART:")
for a, b in negatives:
    ka, kb = ce._dedupe_vendor_key(a), ce._dedupe_vendor_key(b)
    good = ka != kb
    ok &= good
    print(f"  {'OK  ' if good else 'FAIL'} {a!r} -> {ka!r}  |  {b!r} -> {kb!r}")
print("\nALL GOOD" if ok else "\nSOME FAILED -- paste this back")
