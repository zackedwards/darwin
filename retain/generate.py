"""Synthetic 20-year mbox for the demo and for trying the tool.  Not part of the product.

There is no real export to test against, so this builds one that looks like a
small city's shared inbox:

    kind        share   what it exercises
    calendar     25%    stage 2, text/calendar part
    list         15%    stage 2, List-* headers and Precedence: bulk
    system       10%    stage 2, no-reply / alerts / notifications senders
    auto          5%    stage 2, Auto-Submitted header
    courtesy     10%    stage 3, one-line reply quoting a scheduling thread (tests strip_quotes)
    chatter       5%    stage 3, social notes with no business content
    record       20%    stage 3, one realistic message per policy series
    ambiguous     5%    stage 3, vague follow-ups the model should call UNSURE
    sensitive     5%    stage 1, planted SSN / Luhn-valid test card / MRN

Plus, on a fresh build of 20+ messages: two messages with no Date header and
one Message-ID that appears twice, to exercise the REVIEW and de-duplication
paths.

Dates follow a triangular distribution over 20 years with the mode at today,
so most mail is recent, as in a real archive.  Everything is seeded; the same
--seed produces the same archive byte for byte except for the timestamps,
which are relative to now.

The sensitive values are the canonical test values: 123-45-6789 is the SSA's
own example SSN, 4111 1111 1111 1111 is the Visa test card every payment
processor publishes.  Neither belongs to anyone.
"""
from __future__ import annotations

import mailbox
import os
import random
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import format_datetime

DOMAIN = "cityofexample.gov"
STAFF = [("Maria Lopez", "m.lopez"), ("James Carter", "j.carter"), ("Priya Shah", "p.shah"), ("Tom Nguyen", "t.nguyen"),
         ("Dana Whitfield", "d.whitfield"), ("Luis Ortega", "l.ortega"), ("Ann Becker", "a.becker"), ("Ray Kim", "r.kim")]
VENDORS = ["acme-paving.com", "brightwater-eng.com", "fleetpro-services.com", "civicsoft.io"]
TOPICS = ["Budget review", "Public works weekly", "Safety committee", "Vendor check-in", "Q3 planning", "Staff meeting",
          "Project kickoff", "1:1", "Training session", "Site visit"]

# Weighted draw: list length 100, so each repeat is one percent.
KINDS = ["calendar"] * 25 + ["list"] * 15 + ["system"] * 10 + ["auto"] * 5 + ["courtesy"] * 10 + ["chatter"] * 5 + ["record"] * 20 + ["ambiguous"] * 5 + ["sensitive"] * 5

# One or two templates per policy series.  The first field is only a hint for
# whoever reads this file; the model never sees it and must work the series out
# from the text.  Placeholders ({n}, {yy}, {name}...) are filled by _fill().
RECORDS = [  # (series hint, sender kind, subject, body, attachment filename or None)
    ("CONTRACTS", "vendor", "Amendment {n} to paving contract 20{yy}-PW-{n2}",
     "Attached is the executed amendment extending the term through June 30 and adjusting unit pricing per Exhibit B. Please route for the City Manager's signature and return a countersigned copy.", "Amendment_{n}_20{yy}-PW-{n2}.pdf"),
    ("CONTRACTS", "purchasing", "Award notice - RFP 20{yy}-{n2} janitorial services",
     "Council approved the award to the lowest responsive bidder at last night's meeting. Contract term is three years with two one-year options. Draft agreement attached for legal review.", "RFP_20{yy}-{n2}_Agreement_Draft.docx"),
    ("PERSONNEL", "hr", "Performance evaluation - {name} FY20{yy}",
     "Attached is the completed annual evaluation for {name}, signed by the employee and supervisor. Overall rating: Meets Expectations. Please file in the personnel record.", "Evaluation_{last}_FY20{yy}.pdf"),
    ("PERSONNEL", "hr", "Leave of absence approval - {name}",
     "This confirms approval of FMLA leave for {name} beginning the 14th for up to 12 weeks. Return-to-work documentation will be required. Payroll has been notified.", None),
    ("DISBURSEMENT", "vendor", "Invoice INV-{n}{n2} for fleet maintenance",
     "Please find attached invoice INV-{n}{n2} for March preventive maintenance on units 112, 118 and 121. Total due $4,310.22, net 30, referencing PO 20{yy}-0{n2}.", "INV-{n}{n2}.pdf"),
    ("DISBURSEMENT", "finance", "PO 20{yy}-0{n2} approved - traffic signal parts",
     "Purchase order 20{yy}-0{n2} has been approved and issued to the vendor for $12,840.00. Receiving report must be attached before payment release.", "PO_20{yy}-0{n2}.pdf"),
    ("LITIGATION", "legal", "Claim #{n}{n2} - slip and fall at Civic Center",
     "The City Attorney's office has received a notice of claim regarding an incident on the Civic Center steps. All related records, including maintenance logs and incident reports, must be preserved. A litigation hold is now in effect.", None),
    ("INFO_REQUEST", "public", "Public records request - council emails Jan-Mar",
     "Under Chapter 119, Florida Statutes, I request copies of all emails sent or received by council members between January 1 and March 31 regarding the downtown parking garage. Electronic copies are preferred.", None),
    ("MINUTES", "clerk", "Draft minutes - Council regular meeting {mon} {day}",
     "Attached are the draft minutes for approval at the next regular meeting. Resolution 20{yy}-{n2} (stormwater fee adjustment) adopted 5-0; Ordinance 20{yy}-0{n} first reading held.", "Minutes_Council_20{yy}_{mon}.docx"),
    ("PERMITS", "inspections", "Permit BP-{n}{n2} - electrical inspection results",
     "Rough electrical inspection for permit BP-{n}{n2} at 410 Elm St FAILED. Correction required: panel grounding and conductor sizing per NEC 250. Reschedule once corrections are complete.", "Inspection_BP-{n}{n2}.pdf"),
    ("GRANTS", "finance", "FY20{yy} CDBG grant B-{yy}-MC-12-00{n} - quarterly report",
     "The Q3 narrative and financial report for grant B-{yy}-MC-12-00{n} is attached for submission to HUD by the 30th. Drawdown to date is $218,400 of $350,000 awarded.", "CDBG_Q3_Report_FY{yy}.xlsx"),
    ("POLICY_CORR", "staff", "Proposed change to remote work policy",
     "Following the department heads meeting, the recommendation is to amend Section 4.2 to allow up to two remote days per week for eligible positions. Decision needed from the City Manager before the next council agenda is finalized.", None),
    ("ADMIN_CORR", "staff", "Updated office supply ordering procedure",
     "Starting next month all supply orders go through the new procurement portal. Attach your department cost code and get supervisor approval before submitting. Orders over $500 still require a requisition.", None),
]

# Newsletters: one internal, three external, including a vendor promo.
LIST_SOURCES = [("GovTech Weekly", "news@govtechweekly.com", "GovTech Weekly: 5 trends in municipal IT"),
                ("City Manager's Update", f"newsletter@{DOMAIN}", "City Manager's Update - this week at City Hall"),
                ("FL League of Cities", "updates@flcities.org", "Legislative update and upcoming webinars"),
                ("Vendor Hub", "promo@vendorhub.io", "Spring savings on office furniture - 20% off")]
# Machine senders; the local parts are chosen to hit each alternative in classify._SYSTEM_LOCAL.
SYSTEM_MSGS = [(f"noreply@{DOMAIN}", "Your password expires in 7 days", "Your network password will expire in 7 days. Press Ctrl+Alt+Del and choose Change Password."),
               (f"alerts@{DOMAIN}", "Backup job FileServer01 completed", "Job completed successfully. 1.2 TB processed, 0 errors."),
               (f"notifications@{DOMAIN}", "Ticket #{n}{n2} updated", "Your help desk ticket has been updated: Status changed to Resolved."),
               ("do-not-reply@civicsoft.io", "Scheduled maintenance this Saturday", "The permitting portal will be unavailable Saturday 1-3 AM for maintenance.")]
CHATTER = [("Lunch Thursday?", "A few of us are going to the taco place Thursday at noon, want to come?"),
           ("Coffee machine", "The machine on 2 is out of pods again. I put in a facilities request."),
           ("Parking reminder", "Just a reminder the north lot is closed tomorrow for restriping, use the garage."),
           ("Are you free at 3?", "Quick question about the slide deck, do you have 10 min at 3?")]
COURTESY = ["Thanks!", "Sounds good, see you then.", "Got it, thank you.", "Will do.", "Perfect, thanks for the quick turnaround.", "Noted."]
# Vague enough that a careful human could not classify them either; the right answer is UNSURE -> REVIEW.
AMBIGUOUS = [("Re: that thing from Tuesday", "Did you get a chance to look at it? Let me know what you think."),
             ("Question", "Can you send me the file when you have a second?"),
             ("Following up", "Circling back on this. Any update?")]
# One per stage-1 detector: SSN, Luhn-valid card, PHI (MRN + several health keywords).
SENSITIVE = [("Re: benefits enrollment", "Here is the information you asked for to finish my enrollment. My SSN is 123-45-6789 and my date of birth is on the form attached. Thanks."),
             ("Payment for permit fee", "Please charge the permit fee to my card, 4111 1111 1111 1111, exp 09/27. Let me know when it's processed."),
             ("Return to work clearance", "Patient MRN: 4482913. Diagnosis: lumbar strain. Treatment plan: light duty for 2 weeks per the clinic. Lab result pending. Medical record attached.")]


def _staff(r):
    """Random staff member as (display name, 'Name <addr>')."""
    n, u = r.choice(STAFF)
    return n, f"{n} <{u}@{DOMAIN}>"


def _date(r, now, recent_only=False):
    """Triangular over 20 years with mode at now; or within the last 3 days for --append."""
    if recent_only:
        return now - timedelta(days=r.uniform(0, 3))
    return now - timedelta(days=r.triangular(0, 20 * 365, 0), seconds=r.uniform(0, 86400))


def _fill(s, r, name):
    """Substitute template placeholders with plausible random values.  None passes through (no attachment)."""
    if s is None:
        return None
    return s.format(n=r.randint(1, 9), n2=r.randint(10, 99), yy=r.randint(5, 25), name=name, last=name.split()[-1],
                    mon=r.choice(["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Sep", "Oct", "Nov"]), day=r.randint(1, 28))


def _base(frm, to, subject, date, body, mid, r):
    """A plain text message with the standard headers.  date=None leaves the Date header off."""
    m = EmailMessage()
    m["From"], m["To"], m["Subject"], m["Message-ID"] = frm, to, subject, f"<{mid}@{DOMAIN}>"
    if date is not None:
        m["Date"] = format_datetime(date)
    m.set_content(body)
    return m


def make(kind, i, r, now, recent_only=False):
    """Build one EmailMessage of the given kind.  `i` only disambiguates the Message-ID."""
    name, me = _staff(r)
    _, other = _staff(r)
    date = _date(r, now, recent_only)
    mid = f"{int(date.timestamp())}.{i}.{r.randint(1000, 9999)}"
    if kind == "calendar":
        # Real invites carry the event as a text/calendar part; that part, not the subject, is what stage 2 keys on.
        topic = r.choice(TOPICS)
        verb = r.choice(["Accepted", "Declined", "Tentative", "Invitation"])
        m = _base(me, other, f"{verb}: {topic}", date, f"{verb}: {topic}", mid, r)
        ics = f"BEGIN:VCALENDAR\nVERSION:2.0\nBEGIN:VEVENT\nSUMMARY:{topic}\nDTSTART:{date:%Y%m%dT%H%M%S}\nEND:VEVENT\nEND:VCALENDAR\n"
        m.add_attachment(ics.encode(), maintype="text", subtype="calendar", filename="invite.ics")
        return m
    if kind == "list":
        nm, addr, subj = r.choice(LIST_SOURCES)
        m = _base(f"{nm} <{addr}>", me, subj, date, "This week's headlines and upcoming events. Read the full issue online.\n\nTo unsubscribe, click here.", mid, r)
        m["List-Unsubscribe"] = f"<mailto:unsub@{addr.split('@')[1]}>"
        m["List-Id"] = f"<{nm.lower().replace(' ', '-')}.{addr.split('@')[1]}>"
        m["Precedence"] = "bulk"
        return m
    if kind == "system":
        addr, subj, body = r.choice(SYSTEM_MSGS)
        return _base(addr, me, _fill(subj, r, name), date, body, mid, r)
    if kind == "auto":
        m = _base(me, other, f"Automatic reply: {r.choice(TOPICS)}", date, "I am out of the office until Monday with limited access to email. For urgent matters contact the front desk.", mid, r)
        m["Auto-Submitted"] = "auto-replied"
        return m
    if kind == "courtesy":
        # The quoted part below the reply is the trap: without strip_quotes the model would see scheduling text and might call it a record.
        topic = r.choice(TOPICS)
        body = f"{r.choice(COURTESY)}\n\nOn {date:%a, %b %d, %Y}, {other} wrote:\n> Can we move the {topic.lower()} to 2pm? The room is double booked.\n> Let me know."
        return _base(me, other, f"Re: {topic}", date, body, mid, r)
    if kind == "chatter":
        subj, body = r.choice(CHATTER)
        return _base(me, other, subj, date, body, mid, r)
    if kind == "ambiguous":
        subj, body = r.choice(AMBIGUOUS)
        return _base(me, other, subj, date, body, mid, r)
    if kind == "sensitive":
        subj, body = r.choice(SENSITIVE)
        # SSN and PHI come from staff (to HR / to a colleague); the card number comes from a resident.
        frm = me if "MRN" in body or "SSN" in body else f"Resident <{name.split()[0].lower()}.{r.randint(10, 99)}@gmail.com>"
        return _base(frm, f"hr@{DOMAIN}" if "SSN" in body else me, subj, date, body, mid, r)
    # record: pick a template, choose a sender that fits its kind, fill placeholders, attach a stub file if the template names one.
    _, fk, subj, body, att = r.choice(RECORDS)
    frm = {"vendor": f"Accounts <ar@{r.choice(VENDORS)}>", "purchasing": f"Purchasing <purchasing@{DOMAIN}>", "hr": f"Human Resources <hr@{DOMAIN}>",
           "finance": f"Finance <finance@{DOMAIN}>", "legal": f"City Attorney <legal@{DOMAIN}>", "public": f"Resident <{name.split()[0].lower()}{r.randint(10, 99)}@gmail.com>",
           "clerk": f"City Clerk <clerk@{DOMAIN}>", "inspections": f"Inspections <inspections@{DOMAIN}>", "staff": me}[fk]
    m = _base(frm, other, _fill(subj, r, name), date, _fill(body, r, name), mid, r)
    if att:
        fn = _fill(att, r, name)
        # A few bytes with a PDF magic number; only the filename matters to the classifier.
        m.add_attachment(b"%PDF-1.4 synthetic attachment\n", maintype="application", subtype="octet-stream", filename=fn)
    return m


def build(path: str, count: int, seed: int, append: bool) -> int:
    """Write `count` messages to the mbox at `path`.  Returns the mbox's total message count afterwards.

    append=False  wipe the file and build a fresh archive, plus the planted edge cases
    append=True   add `count` messages dated in the last 3 days to the existing file, to
                  simulate a day of new mail for the incremental-run demo.  Uses a
                  different seed so the appended mail is not a replay of the first messages.
    """
    r = random.Random(seed if not append else seed + 1_000_003)
    now = datetime.now(timezone.utc)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    box = mailbox.mbox(path)
    if not append:
        box.clear()
    start = len(box)
    for i in range(count):
        m = make(r.choice(KINDS), start + i, r, now, recent_only=append)
        box.add(m)
    if not append and count >= 20:
        # Edge cases: two messages without a Date header (-> REVIEW, "no Date header"),
        # and one message added twice with the same Message-ID (second copy is skipped as seen).
        for i in range(2):
            m = make("chatter", 9000 + i, r, now)
            del m["Date"]
            box.add(m)
        dup = make("record", 9100, r, now)
        box.add(dup)
        box.add(dup)
    box.flush()
    box.close()
    return len(mailbox.mbox(path))
