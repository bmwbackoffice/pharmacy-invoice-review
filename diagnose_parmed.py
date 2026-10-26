"""READ-ONLY: how the Sales tax page resolves Parmed spellings."""
import classify_emails as ce
import web_db
ce.VENDOR_STD = ce.load_vendor_standardization()
lk = ce.VENDOR_STD.get("lookup") or {}
for name in ("Parmed", "ParMed Pharmaceuticals", "Parmed Pharmaceuticals"):
    k = ce._norm_vendor_std_key(name)
    print(f"{name!r}: key={k!r} exact={lk.get(k)!r} fuzzy={ce._best_fuzzy_vendor_match(k, lk)}")
print("Vendor names entries containing 'parmed':", sorted({v for kk, v in lk.items() if 'parmed' in kk}))
for g in web_db.list_sales_tax_bills("all"):
    if "parmed" in g["vendor"].lower():
        print("group:", g["vendor"], "| std:", g["standard_vendor"], "| bills:", len(g["bills"]),
              "| spellings:", sorted({b["vendor_matched"] for b in g["bills"]}))
