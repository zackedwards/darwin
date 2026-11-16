"""Per-message pipeline: raw mbox bytes -> Msg -> sensitivity flags -> header rule -> local model -> decision.

This module is the whole "brain" of the tool. Everything else (store, report,
__main__) is plumbing around the two public entry points here:

    parse(raw, mbox_key) -> Msg          turn one mbox message into the fields we need
    classify_one(msg, policy, model, now) -> dict
                                         run the three stages and the decision table

The three stages run in a fixed order and each one can end the pipeline:

    1. scan_sensitive()   regex for SSN / card / PHI.  Any hit -> REVIEW_SENSITIVE,
                          and the message is NEVER sent to the model.
    2. header_rule()      four mechanical transitory types that headers prove with
                          certainty (calendar, auto-reply, list, system).  Free.
    3. Model.classify()   Ollama on loopback for everything else.  Returns
                          RECORD / TRANSITORY / UNSURE plus a series id.

Then decide() maps (class, flags, date) to one of six decisions.  See the
decision table in decide() and DESIGN.md.

Data-residency note: `Model` is the only code in the tool that opens a socket,
and its constructor refuses any host that is not loopback.  Sensitive content
never reaches it because stage 1 runs first.
"""
from __future__ import annotations

import email
import email.policy
import email.utils
import hashlib
import html.parser
import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

# Cap on decoded body text we keep in memory per message.  Newsletters and HTML
# mail can run to hundreds of KB; nothing past 20k chars changes a decision.
BODY_CAP = 20_000

# The only headers stage 2 looks at.  Kept as a tuple so parse() copies just
# these into Msg.headers instead of the whole header block.
WATCH_HEADERS = ("list-unsubscribe", "list-id", "precedence", "auto-submitted", "in-reply-to")


# ---------------------------------------------------------------- features
@dataclass
class Msg:
    """Everything the pipeline needs from one email, and nothing else.

    Bodies live here only for the duration of classification; store.insert()
    never writes them.  Two body fields exist because the model should judge a
    reply on its own words, not on the quoted thread beneath it, while the
    sensitivity scan must see everything including quoted text.
    """
    key: str            # normalized Message-ID, or sha256 of raw bytes when absent (stable across runs)
    mbox_key: int       # position in the mbox for this run only; removal renumbers, so store refreshes it
    date: datetime | None   # tz-aware; None when the Date header is missing or unparseable
    from_addr: str      # bare lowercase address, e.g. "noreply@cityofexample.gov"
    subject: str        # whitespace-collapsed
    size: int           # raw bytes, for the size totals in the report
    content_types: set  # every part's MIME type; "text/calendar" is what stage 2 wants
    attachments: list   # filenames only; attachment bodies are never read
    headers: dict       # lowercase name -> value, WATCH_HEADERS only
    body: str           # full decoded text (plain, or HTML stripped to text), capped
    body_own: str       # body with quoted replies removed; this is what the model sees


class _Text(html.parser.HTMLParser):
    """Minimal HTML -> text: collect text nodes, skip <script> and <style> contents."""

    def __init__(self):
        super().__init__()
        self.out, self.skip = [], 0   # skip is a depth counter so nested script/style tags balance

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip:
            self.out.append(data)


def html_to_text(s: str) -> str:
    """Strip tags and collapse whitespace.  Stdlib only; good enough for classification."""
    p = _Text()
    p.feed(s)
    return " ".join(" ".join(p.out).split())


# Lines that mark the start of a quoted earlier message in the three common
# styles: "On <date>, <name> wrote:", Outlook's "-----Original Message-----",
# and a bare "From: ..." header block pasted into the body.
_QUOTE_START = re.compile(r"^(On .{5,120}wrote:|-{3,}\s*Original Message\s*-{3,}|From: .+)\s*$", re.M)


def strip_quotes(text: str) -> str:
    """Return only the author's own text: cut at the first quote marker, drop '>' lines.

    Why: a one-line "Thanks!" that quotes a contract amendment must be judged
    as the courtesy reply it is.  The parent message is its own record and is
    classified separately.
    """
    m = _QUOTE_START.search(text)
    if m:
        text = text[: m.start()]
    return "\n".join(l for l in text.splitlines() if not l.lstrip().startswith(">")).strip()


def parse(raw: bytes, mbox_key: int) -> Msg:
    """Parse one raw RFC 5322 message into a Msg.  Never raises on malformed mail; fields degrade to empty/None."""
    m = email.message_from_bytes(raw, policy=email.policy.default)

    # Stable identity across runs.  Message-ID is normalized (angle brackets off,
    # lowercased) so the same message exported twice matches.  When it is
    # missing we hash the bytes, which is stable as long as the export is.
    mid = str(m.get("Message-ID") or "").strip().strip("<>").lower()
    key = mid or hashlib.sha256(raw).hexdigest()

    # Date drives the transitory window and retain_until, so a bad date must
    # become None (-> REVIEW in the decision table) rather than a guess.
    date = None
    try:
        if m.get("Date"):
            d = email.utils.parsedate_to_datetime(str(m["Date"]))
            date = d if d.tzinfo else d.replace(tzinfo=timezone.utc)  # naive dates are treated as UTC
    except (TypeError, ValueError):
        pass

    addrs = email.utils.getaddresses([str(m.get("From", ""))])
    from_addr = (addrs[0][1] if addrs else "").lower()
    subject = " ".join(str(m.get("Subject", "")).split())

    # Prefer the text/plain alternative; fall back to HTML stripped to text.
    body = ""
    part = m.get_body(preferencelist=("plain", "html"))
    if part is not None:
        try:
            body = part.get_content()
        except Exception:  # undecodable charset or broken transfer encoding; treat as empty
            body = ""
        if part.get_content_type() == "text/html":
            body = html_to_text(body)
    body = body[:BODY_CAP]

    atts = [p.get_filename() for p in m.iter_attachments()]
    return Msg(
        key=key, mbox_key=mbox_key, date=date, from_addr=from_addr, subject=subject, size=len(raw),
        content_types={p.get_content_type() for p in m.walk()},
        attachments=[a for a in atts if a],
        headers={h: str(m[h]) for h in WATCH_HEADERS if m[h] is not None},
        body=body, body_own=strip_quotes(body),
    )


# ---------------------------------------------------------------- stage 1: sensitivity
# SSN: dashed form only (###-##-####).  Undashed 9-digit runs collide with phone
# numbers, invoice numbers and account numbers far too often.  The negative
# lookaheads exclude prefixes the SSA never issues (000, 666, 900-999), a 00
# group, and a 0000 serial.
_SSN = re.compile(r"\b(?!000|666|9\d{2})\d{3}-(?!00)\d{2}-(?!0000)\d{4}\b")

# Card: 13 to 19 digits, optionally separated by single spaces or dashes.  The
# regex is deliberately loose (it also matches long invoice numbers); luhn()
# below is what makes the match meaningful.  Lookarounds stop it matching the
# middle of a longer digit run.
_CARD = re.compile(r"(?<![\d-])(?:\d[ -]?){12,18}\d(?![\d-])")

# PHI has no single token shape, so three signals are combined in scan_sensitive():
#   an explicit medical record number,
#   an ICD-10 code (letter, two digits, dot, 1-4 digits; U is reserved and excluded) near a health keyword,
#   or three or more distinct health keywords in one message.
_MRN = re.compile(r"\bMRN\s*[:#]?\s*\d{5,}\b", re.I)
_ICD = re.compile(r"\b[A-TV-Z]\d{2}\.\d{1,4}\b")
_HEALTH = ("patient", "diagnosis", "prescription", "treatment plan", "medical record", "hipaa", "clinic", "lab result")


def luhn(digits: str) -> bool:
    """Luhn mod-10 checksum, as used by every major card network.  `digits` is digits only."""
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2:           # double every second digit from the right
            n = n * 2
            if n > 9:
                n -= 9      # same as summing the two digits of the product
        total += n
    return total % 10 == 0


def scan_sensitive(text: str) -> list[str]:
    """Return flag names ("SSN", "PCI_CARD", "PHI") found in `text`.

    Only names are returned.  The matched values are never returned, logged or
    stored anywhere; the audit trail records that an SSN was present, not what
    it was.
    """
    flags = []
    if _SSN.search(text):
        flags.append("SSN")
    for m in _CARD.finditer(text):
        d = re.sub(r"\D", "", m.group())
        if 13 <= len(d) <= 19 and luhn(d):
            flags.append("PCI_CARD")
            break               # one flag per message is enough; no need to count cards
    low = text.lower()
    kw = sum(k in low for k in _HEALTH)
    if _MRN.search(text) or (kw and _ICD.search(text)) or kw >= 3:
        flags.append("PHI")
    return flags


# ---------------------------------------------------------------- stage 2: header rules
# Local parts (the bit before @) that machines use.  Anchored so "noreply-team"
# or a person called "alerts" at some domain does not match.
_SYSTEM_LOCAL = re.compile(r"^(no-?reply|do-?not-?reply|notifications?|alerts?|mailer-daemon|postmaster|bounces?)$", re.I)
# Subject prefixes Exchange/Gmail put on out-of-office replies.
_AUTO_SUBJ = re.compile(r"^(automatic reply|out of office|autoreply)", re.I)

# Human-readable reason stored with each header decision.  The category id is
# the dict key; the report groups by it.
HEADER_REASONS = {
    "CALENDAR": "text/calendar part: meeting invitation or response",
    "AUTO_REPLY": "Auto-Submitted header or out-of-office subject",
    "BULK_LIST": "List-Unsubscribe/List-Id header or bulk precedence",
    "SYSTEM": "sender is a no-reply / notification address",
}


def header_rule(msg: Msg) -> str | None:
    """Return a transitory category id when headers prove the message is mechanical mail, else None.

    These four types are decided here rather than by the model because the
    headers are definitive, the rules cost nothing, and together they cover
    roughly half of a typical government inbox.  Order matters only in that a
    calendar invite from a no-reply address is reported as CALENDAR.
    """
    if "text/calendar" in msg.content_types:
        return "CALENDAR"
    # RFC 3834: any Auto-Submitted value other than "no" means a machine sent it.
    if msg.headers.get("auto-submitted", "no").lower() != "no" or _AUTO_SUBJ.match(msg.subject):
        return "AUTO_REPLY"
    # RFC 2369 / 2919 list headers, or the de-facto Precedence: bulk|list.
    if "list-unsubscribe" in msg.headers or "list-id" in msg.headers or msg.headers.get("precedence", "").lower() in ("bulk", "list"):
        return "BULK_LIST"
    if _SYSTEM_LOCAL.match(msg.from_addr.split("@")[0]):
        return "SYSTEM"
    return None


# ---------------------------------------------------------------- stage 3: local model
# The prompt is an ordered decision procedure, not an open question, because a
# 3B model answers "is this transitory? if not, is it a record?" far more
# consistently than "what is this?".  The three examples anchor each class.
# {series} is filled from policy.json at startup so adding a series to the
# policy file is all it takes to make the model aware of it.  Double braces
# are literal braces in str.format().
PROMPT = """You are a records management assistant for a local government office. Classify ONE email under the records retention schedule. Quoted earlier messages have been removed; judge only the text shown.

Decide in this order:
1. TRANSITORY: the text is only scheduling, an acknowledgement or thank-you, a reminder, a social or personal note, a "did you see this" nudge, a request to send a file, or a short reply with no business content of its own. A short reply inside a thread about a record is still TRANSITORY; the original message is the record.
2. RECORD: the text itself documents agency business: a decision, approval, transaction, payment, contract, obligation, personnel action, permit or inspection result, legal matter, a request from a member of the public, a policy or procedure, or a substantive work discussion. Pick the single best series id:
{series}
3. UNSURE: you cannot tell what the email is about.

Examples:
- "Sounds good, see you at 2." -> {{"klass": "TRANSITORY", "series": null, "reason": "acknowledgement with no business content"}}
- "Attached is the executed amendment extending the term through June 30." -> {{"klass": "RECORD", "series": "CONTRACTS", "reason": "documents a contract amendment"}}
- "Did you get a chance to look at it? Let me know what you think." -> {{"klass": "UNSURE", "series": null, "reason": "cannot tell what is being discussed"}}

Email:
From: {sender}
Subject: {subject}
Attachments: {attachments}
Body:
{body}

Answer with JSON only, exactly this shape:
{{"klass": "RECORD" or "TRANSITORY" or "UNSURE", "series": "<series id>" or null, "reason": "<one short sentence>"}}"""

# How much of the author's own text the model sees.  Enough for any real
# business email; keeps prompt tokens, and therefore latency, bounded.
MODEL_BODY_CHARS = 1500


class Model:
    """Thin client for Ollama's /api/generate.  The only network code in the tool.

    The constructor pins the host to loopback.  That check is the enforcement
    of the "HIPAA, PII and PCI cannot leave the tenant" requirement: there is no
    flag, environment variable or config key that can point this at a remote
    endpoint.  Someone who wants that has to edit this file.

    Tests substitute any object with a .classify(msg) -> dict method.
    """

    def __init__(self, url: str, name: str, policy: dict):
        self.url, self.name = url.rstrip("/"), name
        self.series_ids = [s["id"] for s in policy["series"]]
        self.default_series = policy["default_series"]
        # One bullet per series, built once; this is the {series} block of PROMPT.
        self.series_text = "\n".join(f"- {s['id']}: {s['name']}. {s['description']}" for s in policy["series"])
        # Pull the bare host out of http://host:port/path without urllib.parse,
        # which would accept odd forms like "http://127.0.0.1@evil.com".
        host = self.url.split("//", 1)[-1].split("/")[0].split(":")[0]
        if host not in ("127.0.0.1", "localhost", "::1"):
            raise SystemExit(f"Refusing model endpoint {self.url}: email content may only go to a model on this machine.")

    def check(self) -> None:
        """Fail fast before the scan: Ollama must be reachable and the model must be pulled."""
        try:
            with urllib.request.urlopen(self.url + "/api/tags", timeout=5) as r:
                names = {m["name"] for m in json.load(r).get("models", [])}
        except (urllib.error.URLError, OSError) as e:
            raise SystemExit(f"Cannot reach Ollama at {self.url} ({e}). Install Ollama and make sure it is running.")
        # Ollama reports "llama3.2:3b" as-is but a bare "llama3.2" as "llama3.2:latest".
        if self.name not in names and self.name + ":latest" not in names:
            raise SystemExit(f"Model {self.name} is not installed. Run:  ollama pull {self.name}")

    def classify(self, msg: Msg) -> dict:
        """Ask the model about one message.  Returns {"klass", "series", "reason"}; never raises.

        Validation is strict because the model's text is untrusted input to the
        decision table: an unknown klass triggers a retry, an unknown series on a
        RECORD falls back to the policy's default series (keeps the record rather
        than dropping it), and two bad answers become UNSURE so a human looks.
        """
        prompt = PROMPT.format(
            series=self.series_text, sender=msg.from_addr, subject=msg.subject,
            attachments=", ".join(msg.attachments) or "none", body=msg.body_own[:MODEL_BODY_CHARS] or "(empty)",
        )
        # format=json makes Ollama constrain output to valid JSON; temperature 0
        # makes the same message get the same answer on every run.
        payload = json.dumps({"model": self.name, "prompt": prompt, "format": "json", "stream": False,
                              "options": {"temperature": 0}}).encode()
        for _ in range(2):  # one retry on bad output
            try:
                req = urllib.request.Request(self.url + "/api/generate", data=payload, headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=180) as r:   # 180s covers a cold model load
                    out = json.loads(json.load(r)["response"])
                klass = str(out.get("klass", "")).upper()
                if klass not in ("RECORD", "TRANSITORY", "UNSURE"):
                    continue
                series = out.get("series") if klass == "RECORD" else None
                if klass == "RECORD" and series not in self.series_ids:
                    series = self.default_series
                reason = " ".join(str(out.get("reason", "")).split())[:300] or "model gave no reason"
                return {"klass": klass, "series": series, "reason": reason}
            except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError, AttributeError):
                continue    # network blip, non-JSON response, or JSON of the wrong shape
        return {"klass": "UNSURE", "series": None, "reason": "model returned unusable output twice"}


# ---------------------------------------------------------------- decision
def decide(klass: str, flags: list, date: datetime | None, now: datetime, policy: dict, series: str | None):
    """Decision table.  First matching row wins.  Returns (decision, retain_until ISO date or None).

        flags present            -> REVIEW_SENSITIVE   human looks; never auto-proposed
        RECORD                   -> RETAIN             retain_until = date + series years; None if permanent
        no date                  -> REVIEW             cannot age it, cannot compute retention
        TRANSITORY, > window     -> DELETE_PROPOSED    the only decision that pre-fills "delete"
        TRANSITORY, <= window    -> RETAIN_PENDING     ages into DELETE_PROPOSED on a later run
        UNSURE                   -> REVIEW

    RECORD is checked before the date test on purpose: an undated record is
    still kept, with no computed end date, rather than sent to review.

    ponytail: retention uses 365-day years, not fiscal-year boundaries.  GS1-SL
    counts "fiscal years after X"; a production version would round up to the
    agency's fiscal year end.  The data to do that is here (date + years).
    """
    if flags:
        return "REVIEW_SENSITIVE", None
    if klass == "RECORD":
        years = next((s["retention_years"] for s in policy["series"] if s["id"] == series), None)
        until = (date + timedelta(days=365 * years)).date().isoformat() if (date and years) else None
        return "RETAIN", until
    if date is None:
        return "REVIEW", None
    if klass == "TRANSITORY":
        age = (now - date).days
        return ("DELETE_PROPOSED" if age > policy["transitory"]["window_days"] else "RETAIN_PENDING"), None
    return "REVIEW", None


def classify_one(msg: Msg, policy: dict, model, now: datetime) -> dict:
    """Run the three stages and the decision table for one message.

    `model` is anything with .classify(msg) -> {klass, series, reason}; tests
    pass a fake.  Returns the dict store.insert() writes:

        klass        RECORD | TRANSITORY | UNSURE
        category     series id for records; CALENDAR/AUTO_REPLY/BULK_LIST/SYSTEM for header
                     decisions; SENSITIVE for stage-1 hits; otherwise the klass itself
        via          "sensitive" | "header" | "model"   which stage decided
        reason       one line for the CSV and the audit row
        flags        list of sensitivity flag names, usually empty
        decision     one of the six decisions
        retain_until ISO date or None
    """
    # Stage 1 sees everything: subject, full body including quoted text, attachment names.
    flags = scan_sensitive(" ".join([msg.subject, msg.body, *msg.attachments]))
    if flags:
        klass, category, via, reason = "UNSURE", "SENSITIVE", "sensitive", "contains " + ", ".join(flags)
    elif rule := header_rule(msg):
        klass, category, via, reason = "TRANSITORY", rule, "header", HEADER_REASONS[rule]
    else:
        out = model.classify(msg)
        klass, via, reason = out["klass"], "model", out["reason"]
        category = out["series"] if klass == "RECORD" else klass
    decision, until = decide(klass, flags, msg.date, now, policy, category if klass == "RECORD" else None)
    if msg.date is None and decision == "REVIEW":
        reason += " (no Date header)"   # tell the director why a plausible transitory message is in review
    return {"klass": klass, "category": category, "via": via, "reason": reason, "flags": flags,
            "decision": decision, "retain_until": until}
