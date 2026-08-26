"""
form_class.py -- does a proposal record represent a clearance being GRANTED, or
an administrative action on a clearance that already exists?

WHY THIS EXISTS
---------------
Filtering on status alone ("EC Granted") counts things that are not new
clearances. Measured on the 8 Aug 2026 snapshot, 7,024 of 42,242 granted EC
records (16.6%) were transfers, amendments, validity extensions, corrigenda,
splittings and surrenders -- a transfer is the same mine changing hands, and a
surrender is a clearance being given up, counted as one being issued.

Worse than the double counting: a transfer record's date is the TRANSFER date.
We display it as the clearance date. So on a map of "clearances granted
2023-2026", a 2019 clearance transferred in 2024 appears as a 2024 grant. That
is wrong on the date axis whether or not the original is also in the data --
and usually it is not: 81.6% of these records have no parent anywhere in our
data, because PARIVESH 2.0 coverage only starts Sept 2022.

WHERE THE CATEGORIES COME FROM
------------------------------
The form_type strings are PARIVESH's own. The categories are the ministry's
too: its EC dashboard API (admin_api/dashboard/public/ecClearanceCount) returns
EC, ToR, EC Amendment, EC Transfer, EC Validity Extension, EC Surrender,
EC Splitting, Corrigendum ... as SEPARATE counters. It reports "EC = 23" and
"EC Transfer = 3" on different lines; it does not add them together.

Our mapping of form strings onto those categories is verified against the
ministry's published counts for central EC, 2025:

    Corrigendum        ours 19  ministry 19   exact
    Validity Extension ours 16  ministry 16   exact
    Surrender          ours  3  ministry  3   exact
    Amendment          ours 116 ministry 117
    Transfer           ours  94 ministry  98
    EC (Form 1 + B2)   ours 466 ministry 501
    -- and for 2023, EC: ours 201, ministry 201, exact.

WHAT IS OURS, AND SHOULD BE READ AS JUDGMENT
--------------------------------------------
The ministry lists its twelve counters side by side and never groups them. The
GRANT/ADMIN split below is OUR interpretation. The test applied is:

    does this event grant a permission that did not previously exist?

which puts a lease RENEWAL (the old lease expired; a new term is granted) on
the GRANT side, and an EC validity EXTENSION (the clearance is still alive, its
clock is pushed out) on the ADMIN side. That line is defensible but arguable,
and no ministry source draws it. FC and CRZ have no external corroboration at
all -- the forest dashboard is organised by stage, not by form.

UNKNOWN
-------
Not a category of anything real -- a sentinel meaning "this string is not in
the table yet". It exists because both alternatives fail silently: a deny-list
counts tomorrow's new form as a clearance (the inflation quietly returns), an
allow-list drops it (silent under-counting). Callers must treat UNKNOWN as a
condition to report and act on, never as a default bucket.
"""

from __future__ import annotations

import re

GRANT = "grant"
ADMIN = "admin"
UNKNOWN = "unknown"

# Punctuation and spacing drift in this field: the data holds BOTH
# "Form-H (Amendment in approval Granted)" (17 rows) and
# "Form H (Amendment in approval Granted)" (1 row). Match on letters and
# digits only so a hyphen going missing does not silently create a new form.
_STRIP = re.compile(r"[^a-z0-9]+")


def _key(s: str) -> str:
    return _STRIP.sub("", (s or "").lower())


# (form_type as PARIVESH writes it, bucket, ministry's category)
_TABLE = [
    # ---- EC -------------------------------------------------------------
    ("Application for EC for Mining of Minor Minerals of Mine Lease (0-5 HA) - Form-2",
     GRANT, "EC"),
    ("Application for EC (Category A, B1, and B2 Violation)- Form 1",
     GRANT, "EC"),
    ("Application for ToR (Category A, B1, and B2 Violation)/EC (Category B2) - Form 1",
     GRANT, "EC"),          # Cat-B2 EC is granted through the combined ToR form
    ("Application for Transfer of EC- Form-7", ADMIN, "EC Transfer"),
    ("Application for Amendment in EC- Form-4", ADMIN, "EC Amendment"),
    ("Application for Validity Extension of EC- Form-6", ADMIN, "EC Validity Extension"),
    ("Application for Corrigendum Form-13", ADMIN, "Corrigendum"),
    ("Application for amendment in ToR (for categories A & B1)/Amendment in EC "
     "(for category B2)- Form-3", ADMIN, "ToR Amendment"),
    ("Application for Surrender of Environmental Clearance - Form -11",
     ADMIN, "EC Surrender"),
    ("Application for Splitting of Environmental Clearance - Form 12",
     ADMIN, "EC Splitting"),
    ("Application for Transfer of ToR - Form-8", ADMIN, "ToR Transfer"),

    # ---- FC -------------------------------------------------------------
    # No external source validates these; the forest dashboard counts by stage
    # (Stage-I / Stage-II / diversion order), not by form.
    ("Form-A (Part-I): Diversion of Forest Land", GRANT, "FC diversion"),
    ("Form-E (Part-I): Re-Diversion i) Land Use Change ii) Laying of Overhead/ "
     "Under Ground OFC/ drinking water pipeline/ slurry pipeline/ electric cable/ "
     "CNG/PNG within RoW", GRANT, "FC re-diversion"),
    ("Form-C (Part-I): For seeking prior approval for Exploration & Survey",
     GRANT, "FC exploration"),
    # Renewal follows expiry, so a new term is granted -> GRANT. 7 of these 8
    # records have no Form-A twin in our data and carry real area (306 ha,
    # 217 ha, 172 ha), so excluding them would erase projects, not de-duplicate.
    ("Form-B (Part-I): Renewal of Lease on Forest Land", GRANT, "FC lease renewal"),
    # Section 2(iii) leasing permission is distinct from the 2(ii) diversion
    # that Form-A obtains; the form carries its own KML, area and lease term.
    ("Form-D (Part-I): Signing of Lease (section 2(iii)) on Forest Land",
     GRANT, "FC lease (s.2(iii))"),
    ("Form-F: Application for Transfer of Lease/Change in User Agency Name",
     ADMIN, "FC transfer"),
    ("Form-H (Amendment in approval Granted)", ADMIN, "FC amendment"),
    ("Form H (Amendment in approval Granted)", ADMIN, "FC amendment"),

    # ---- CRZ ------------------------------------------------------------
    ("Fresh Proposal Form", GRANT, "CRZ"),
    ("Application for CRZ Clearance", GRANT, "CRZ"),
    ("Amendment Proposal Form", ADMIN, "CRZ Amendment"),
    ("Application for Amendment in CRZ Clearance", ADMIN, "CRZ Amendment"),
    ("Transfer of CRZ Clearance", ADMIN, "CRZ Transfer"),
]

_BUCKET: dict[str, str] = {}
_CATEGORY: dict[str, str] = {}
for _text, _bucket, _cat in _TABLE:
    _k = _key(_text)
    # Normalising away punctuation could in principle collapse two genuinely
    # different forms into one key. Fail loudly at import rather than quietly
    # mis-file records for a year.
    if _k in _BUCKET and _BUCKET[_k] != _bucket:
        raise AssertionError(f"form_type key collision with differing buckets: {_text!r}")
    _BUCKET[_k] = _bucket
    _CATEGORY[_k] = _cat


def classify(form_type: str) -> str:
    """GRANT, ADMIN or UNKNOWN. Never guesses."""
    return _BUCKET.get(_key(form_type), UNKNOWN)


def category(form_type: str) -> str:
    """The ministry-style category, for reporting what was excluded and why."""
    return _CATEGORY.get(_key(form_type), "unclassified")
