"""Seeded synthetic database for Quorvia Instruments (fictional).

Everything here is invented: company names are built from a fixed word list,
people's names from fictional first and last names, e-mail addresses use the
reserved ``.example`` domain and phone numbers the fictional 555 range.

A handful of customers are fixed on purpose so that questions can name them:
two companies share the word "Halvorsen", two "Northgate" and two "Aldermoor"
(name collisions for the ambiguity set), and three APAC customers never order.
Generation uses ``random.Random(seed)`` only, so the file is byte-identical
across runs on the same Python version.
"""

from __future__ import annotations

import datetime as dt
import random
import sqlite3
from pathlib import Path

from .schema import ddl

REGIONS = [("EU", "Europe", "EUR"), ("US", "United States", "USD"), ("UK", "United Kingdom", "GBP"),
           ("APAC", "Asia-Pacific", "SGD")]

LINES = [("Pulse", "PU"), ("Tide", "TD"), ("Ember", "EM"), ("Vane", "VN")]
TIERS = [("Lite", "100"), ("Pro", "200"), ("Max", "300")]
PRICES = {"PU": (249.0, 449.0, 799.0), "TD": (389.0, 689.0, 1190.0), "EM": (129.0, 219.0, 399.0),
          "VN": (529.0, 949.0, 1490.0)}
LAUNCH = {"QV-PU100": "2019-03-04", "QV-PU200": "2019-03-04", "QV-PU300": "2021-05-17",
          "QV-TD100": "2018-09-10", "QV-TD200": "2020-02-03", "QV-TD300": "2022-06-20",
          "QV-EM100": "2018-04-23", "QV-EM200": "2019-11-11", "QV-EM300": "2025-04-15",
          "QV-VN100": "2020-08-31", "QV-VN200": "2021-10-04", "QV-VN300": "2024-09-02"}
DISCONTINUED = {"QV-TD100": "2025-06-30", "QV-EM100": "2024-12-31"}  # last day it could be ordered

# fixed customers: (name, region, segment, signed_up_on, weight); weight drives order volume
FIXED_CUSTOMERS = [
    ("Halvorsen Marine", "EU", "enterprise", "2023-02-14", 3.0),
    ("Halvorsen Labs", "US", "smb", "2024-05-06", 1.0),
    ("Northgate Water", "UK", "enterprise", "2022-11-01", 2.5),
    ("Northgate Logistics", "APAC", "mid-market", "2024-03-18", 1.2),
    ("Aldermoor Foods", "EU", "mid-market", "2023-07-03", 1.5),
    ("Aldermoor Health", "US", "enterprise", "2024-01-22", 2.0),
]
NEVER_ORDER = [("Kestrel Grove Farms", "APAC", "smb", "2025-08-11"),
               ("Saltmarsh Analytics", "APAC", "smb", "2026-02-02"),
               ("Tamsin Ridge Utilities", "APAC", "mid-market", "2026-04-27")]

PREFIXES = ["Brightwater", "Calder", "Dunmore", "Elmstead", "Fernhollow", "Glenroy", "Harrowgate", "Ivybridge",
            "Juniper", "Kingsmere", "Larkspur", "Marrow", "Nettlefield", "Oakhurst", "Pellham", "Quillon",
            "Rosewick", "Stonebridge", "Thistledown", "Umberfield", "Valewood", "Westcombe", "Yarrow", "Ashby",
            "Birchmont", "Coldharbour", "Deepdale", "Eastwick", "Foxley", "Greystone", "Hollins", "Ironvale",
            "Kilnworth", "Lowmoor", "Millbrook", "Northam", "Orchard", "Pinecrest", "Redhill", "Silverlake",
            "Tidewater", "Underhill", "Verity", "Whitlow"]
SUFFIXES = ["Utilities", "Logistics", "Foods", "Energy", "Hospitals", "Water", "Labs", "Farms", "Pharma",
            "Data Centers", "Breweries", "Transit", "Facilities", "Cold Storage", "Agritech", "Marine"]

FIRST = ["Ana", "Bram", "Cleo", "Dario", "Edda", "Femi", "Greta", "Hugo", "Ines", "Jory", "Kaia", "Lior",
         "Mara", "Niko", "Odile", "Pavo", "Quin", "Rhea", "Soren", "Tilde", "Ugo", "Vela", "Wren", "Yara"]
LAST = ["Abelard", "Brisk", "Corvel", "Dunlin", "Estrom", "Falk", "Gorrin", "Hessel", "Ivarson", "Jandt",
        "Kesler", "Lunde", "Morrow", "Nadal", "Orrin", "Pask", "Quell", "Rasmund", "Stavig", "Torvik",
        "Ulden", "Velden", "Wexley", "Yarden"]
DEPARTMENTS = [("Sales", 14), ("Support", 12), ("Engineering", 14), ("Operations", 8), ("Finance", 6),
               ("Marketing", 6)]
ROLES = ["Facilities Manager", "Head of Operations", "Procurement Lead", "Lab Director", "IT Manager",
         "Sustainability Officer"]


def _date(d: dt.date) -> str:
    return d.isoformat()


def _rand_date(rng: random.Random, start: str, end: str) -> dt.date:
    a, b = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
    return a + dt.timedelta(days=rng.randint(0, (b - a).days))


def build_rows(seed: int = 42) -> dict[str, list[tuple]]:
    rng = random.Random(seed)
    rows: dict[str, list[tuple]] = {t: [] for t in ("regions", "products", "customers", "orders", "order_items",
                                                     "support_tickets", "employees", "customer_contacts")}
    rows["regions"] = list(REGIONS)

    pid = 0
    products = []
    for lname, code in LINES:
        for t, (tname, tnum) in enumerate(TIERS):
            pid += 1
            sku = f"QV-{code}{tnum}"
            products.append((pid, sku, f"{lname} {tname}", lname, tname, PRICES[code][t], LAUNCH[sku],
                             1 if sku in DISCONTINUED else 0))
    rows["products"] = products

    # customers: fixed ones first, then generated names that never reuse a fixed word
    customers = []
    weights = {}
    cid = 0
    for name, reg, seg, since, w in FIXED_CUSTOMERS:
        cid += 1
        customers.append((cid, name, reg, seg, since))
        weights[cid] = w
    names = sorted({f"{p} {s}" for p in PREFIXES for s in SUFFIXES})
    rng.shuffle(names)
    seg_pool = ["enterprise"] * 2 + ["mid-market"] * 3 + ["smb"] * 5
    reg_pool = ["EU"] * 4 + ["US"] * 4 + ["UK"] * 2 + ["APAC"] * 2
    used_prefix = set()
    for name in names:
        if len(customers) >= 111:
            break
        prefix = name.split()[0]
        if prefix in used_prefix:  # one company per prefix, so no accidental name collisions
            continue
        used_prefix.add(prefix)
        cid += 1
        seg = rng.choice(seg_pool)
        since = _date(_rand_date(rng, "2021-01-01", "2026-03-31"))
        customers.append((cid, name, rng.choice(reg_pool), seg, since))
        weights[cid] = {"enterprise": 2.2, "mid-market": 1.3, "smb": 0.6}[seg] * rng.uniform(0.5, 1.5)
    for name, reg, seg, since in NEVER_ORDER:
        cid += 1
        customers.append((cid, name, reg, seg, since))
    rows["customers"] = customers
    since_of = {c[0]: c[4] for c in customers}

    # orders and order lines
    orderable = [c for c in weights]
    wlist = [weights[c] for c in orderable]
    by_sku = {p[1]: p for p in products}
    oid = 1000
    orders, items = [], []
    for _ in range(2400):
        cust = rng.choices(orderable, weights=wlist)[0]
        start = max("2024-01-01", since_of[cust])
        day = _rand_date(rng, start, "2026-06-30")
        ds = _date(day)
        oid += 1
        age = (dt.date(2026, 6, 30) - day).days
        if age < 10:
            status = rng.choices(["shipped", "cancelled"], [0.9, 0.1])[0]
        else:
            status = rng.choices(["delivered", "cancelled", "returned", "shipped"], [0.82, 0.09, 0.06, 0.03])[0]
        orders.append((oid, cust, ds, status, rng.choices(["web", "sales", "partner"], [0.45, 0.35, 0.2])[0]))
        avail = [p for p in products if p[6] <= ds and (p[1] not in DISCONTINUED or ds <= DISCONTINUED[p[1]])]
        for p in rng.sample(avail, k=min(len(avail), rng.choices([1, 2, 3, 4], [0.45, 0.3, 0.17, 0.08])[0])):
            qty = rng.choices([1, 2, 3, 5, 10, 20], [0.3, 0.25, 0.18, 0.14, 0.09, 0.04])[0]
            disc = rng.choice([0.0, 0.0, 0.05, 0.1, 0.15])
            items.append((oid, p[0], qty, round(p[5] * (1 - disc), 2)))
    rows["orders"], rows["order_items"] = orders, items
    assert by_sku["QV-PU200"][0] == 2

    # tickets
    tickets = []
    for tid in range(1, 901):
        cust = rng.choices(orderable, weights=wlist)[0]
        opened = _rand_date(rng, max("2024-01-01", since_of[cust]), "2026-06-30")
        os_ = _date(opened)
        prod = rng.choice([p for p in products if p[6] <= os_])
        pri = rng.choices(["low", "normal", "high", "urgent"], [0.25, 0.45, 0.2, 0.1])[0]
        cat = rng.choices(["hardware", "calibration", "connectivity", "billing"], [0.3, 0.25, 0.3, 0.15])[0]
        age = (dt.date(2026, 6, 30) - opened).days
        if rng.random() < (0.5 if age < 20 else 0.05):
            closed = None
        else:
            days = {"urgent": 1, "high": 3, "normal": 6, "low": 10}[pri] * rng.uniform(0.3, 2.5)
            c = opened + dt.timedelta(days=int(round(days)))
            closed = _date(min(c, dt.date(2026, 6, 30)))
        tickets.append((tid, cust, prod[0], os_, closed, pri, cat))
    rows["support_tickets"] = tickets

    # employees: department heads first (no manager), then staff reporting to a regional lead
    emps = []
    eid = 0
    used_names = set()

    def person() -> str:
        while True:
            n = f"{rng.choice(FIRST)} {rng.choice(LAST)}"
            if n not in used_names:
                used_names.add(n)
                return n

    def email(n: str) -> str:
        return n.lower().replace(" ", ".") + "@quorvia.example"

    def phone() -> str:
        return f"+1 555 01{rng.randint(10, 99)} {rng.randint(1000, 9999)}"

    base = {"Sales": 98000, "Support": 72000, "Engineering": 128000, "Operations": 84000, "Finance": 105000,
            "Marketing": 90000}
    for dept, n in DEPARTMENTS:
        eid += 1
        head = eid
        nm = person()
        emps.append((eid, nm, email(nm), phone(), dept, "EU", _date(_rand_date(rng, "2016-01-01", "2020-12-31")),
                     int(round(base[dept] * 1.8, -3)), None))
        leads = {}
        for k in range(n - 1):
            eid += 1
            reg = ["EU", "US", "UK", "APAC"][k % 4]
            nm = person()
            mgr = leads.get(reg, head)
            emps.append((eid, nm, email(nm), phone(), dept, reg, _date(_rand_date(rng, "2018-01-01", "2026-05-31")),
                         int(round(base[dept] * rng.uniform(0.8, 1.35), -2)), mgr))
            leads.setdefault(reg, eid)
    rows["employees"] = emps

    contacts = []
    for k, c in enumerate(customers, start=1):
        nm = person()
        contacts.append((k, c[0], nm, nm.lower().replace(" ", "_") + "@customer.example", phone(), rng.choice(ROLES)))
    rows["customer_contacts"] = contacts
    return rows


def build_db(path: str | Path, seed: int = 42) -> dict[str, int]:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    rows = build_rows(seed)
    con = sqlite3.connect(str(path))
    con.executescript(ddl())
    for t, rs in rows.items():
        if rs:
            con.executemany(f"INSERT INTO {t} VALUES ({','.join('?' * len(rs[0]))})", rs)
    con.commit()
    con.execute("VACUUM")
    con.close()
    return {t: len(rs) for t, rs in rows.items()}
