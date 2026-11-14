"""Synthetic demo data. Every name, number and record here is made up.

Dates are generated relative to "today" so the demo never goes stale.
Phone numbers use the 555-01xx range, which is reserved for fiction.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from pathlib import Path

from careflow.ehr import EHR

PATIENTS = [
    (
        "P001",
        "Maria",
        "Lopez",
        "1984-03-12",
        "(555) 010-0101",
        "maria.lopez@example.com",
        "MRN004211",
    ),
    (
        "P002",
        "James",
        "Carter",
        "1971-11-02",
        "(555) 010-0102",
        "j.carter@example.com",
        "MRN004212",
    ),
    ("P003", "Aisha", "Khan", "1992-07-25", "(555) 010-0103", "aisha.k@example.com", "MRN004213"),
    ("P004", "Tom", "Nguyen", "1966-01-30", "(555) 010-0104", "tnguyen@example.com", "MRN004214"),
    (
        "P005",
        "Elena",
        "Petrova",
        "2001-05-18",
        "(555) 010-0105",
        "elena.p@example.com",
        "MRN004215",
    ),
]

PROVIDERS = [
    ("DR-OKAFOR", "Dr. Ngozi Okafor", "Family Medicine"),
    ("DR-SATO", "Dr. Ken Sato", "Dermatology"),
    ("DR-REYES", "Dr. Lucia Reyes", "Cardiology"),
]

SLOT_TIMES = [time(9, 0), time(10, 30), time(14, 0), time(15, 30)]

# (id, patient, medication, dose, refills_remaining, controlled)
PRESCRIPTIONS = [
    ("RX-1001", "P001", "lisinopril", "10 mg daily", 2, False),
    ("RX-1002", "P001", "atorvastatin", "20 mg nightly", 0, False),
    ("RX-1003", "P002", "metformin", "500 mg twice daily", 3, False),
    ("RX-1004", "P002", "oxycodone", "5 mg as needed", 1, True),
    ("RX-1005", "P004", "sertraline", "50 mg daily", 4, False),
    ("RX-1006", "P003", "albuterol", "90 mcg inhaler", 1, False),
]

# (number, patient, days_ago, status, [(description, charge_cents, insurance_paid_cents)])
INVOICES = [
    (
        "INV-1042",
        "P002",
        20,
        "open",
        [
            ("Office visit, established patient", 18500, 14800),
            ("Basic metabolic panel", 6200, 6200),
        ],
    ),
    ("INV-1043", "P003", 35, "paid", [("Dermatology consult", 24000, 19200)]),
    (
        "INV-1050",
        "P003",
        6,
        "open",
        [("Skin biopsy", 31000, 21700), ("Pathology", 14500, 8700)],
    ),
]


def _weekdays(start: date, count: int) -> list[date]:
    days: list[date] = []
    d = start
    while len(days) < count:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


def seed(path: Path | str, today: date | None = None, *, reset: bool = True) -> EHR:
    """Create (or recreate) the mock EHR database and return a handle to it."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if reset and path.exists():
        path.unlink()
    today = today or date.today()
    ehr = EHR(path, today=today)

    with ehr._tx() as conn:
        conn.executemany("INSERT INTO patients VALUES (?, ?, ?, ?, ?, ?, ?)", PATIENTS)
        conn.executemany("INSERT INTO providers VALUES (?, ?, ?)", PROVIDERS)

        days = _weekdays(today + timedelta(days=1), 15)
        for provider_id, *_ in PROVIDERS:
            for d in days:
                for t in SLOT_TIMES:
                    starts = datetime.combine(d, t).isoformat(timespec="minutes")
                    slot_id = f"S-{provider_id[3:6]}-{d:%m%d}-{t:%H%M}"
                    # Leave a believable mix of open and taken slots.
                    is_open = 0 if (d.day + t.hour) % 3 == 0 else 1
                    conn.execute(
                        "INSERT INTO slots VALUES (?, ?, ?, ?)",
                        (slot_id, provider_id, starts, is_open),
                    )

        def book(appt_id: str, patient_id: str, provider_id: str, day: date, t: time) -> None:
            slot_id = f"S-{provider_id[3:6]}-{day:%m%d}-{t:%H%M}"
            starts = datetime.combine(day, t).isoformat(timespec="minutes")
            conn.execute("UPDATE slots SET is_open = 0 WHERE id = ?", (slot_id,))
            conn.execute(
                "INSERT INTO appointments VALUES (?, ?, ?, ?, ?, 'booked')",
                (appt_id, patient_id, provider_id, slot_id, starts),
            )

        book("APT-501", "P001", "DR-OKAFOR", days[1], time(9, 0))
        book("APT-502", "P003", "DR-SATO", days[3], time(14, 0))
        book("APT-503", "P004", "DR-REYES", days[2], time(10, 30))
        book("APT-504", "P004", "DR-OKAFOR", days[8], time(15, 30))

        conn.executemany(
            "INSERT INTO prescriptions VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    rx_id,
                    pid,
                    med,
                    dose,
                    refills,
                    int(ctrl),
                    (today - timedelta(days=28)).isoformat(),
                )
                for rx_id, pid, med, dose, refills, ctrl in PRESCRIPTIONS
            ],
        )
        for number, pid, days_ago, status, lines in INVOICES:
            issued = (today - timedelta(days=days_ago)).isoformat()
            conn.execute("INSERT INTO invoices VALUES (?, ?, ?, ?)", (number, pid, issued, status))
            conn.executemany(
                "INSERT INTO invoice_lines VALUES (?, ?, ?, ?)",
                [(number, desc, charge, paid) for desc, charge, paid in lines],
            )
    return ehr
