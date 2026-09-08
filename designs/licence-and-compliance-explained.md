# Licensing and compliance, explained without the jargon

**Audience:** anyone who needs to understand ADR-0019 without reading it — a
prospect's technical evaluator, a customer's security reviewer, a new
maintainer, or the maintainer six months from now.

**Published page:** a private hosted copy exists for sharing with people who
would rather read a web page than a Markdown file; the maintainer holds the
link. The page and this file carry the same content — **edit both**, or
regenerate the page from this file.

**Companions.** This is the *plain-English* layer. Below it:
[`licence-and-compliance-overview.md`](licence-and-compliance-overview.md) is the
technical orientation and the gap history;
[`funis-vendor-runbook.md`](funis-vendor-runbook.md) is the supplier-side
procedure; [ADR-0019](decisions/0019-licence-and-compliance-attestation.md)
is the decisions and why; `ARCHITECTURE.md` §16.47–16.66 is the as-built detail.

---

## The two questions

Everything here exists to answer two separate questions. Keeping them apart is
what stops the design getting confused.

1. **What is this customer allowed to do?** How many people can use MATOFX at
   once, which features they have, and when that runs out. This is the
   **licence**.
2. **Can they show they stayed inside it, and that their records weren't
   doctored?** This is the **compliance report**, and it is much the harder of
   the two.

The second is hard because MATOFX runs on the customer's own hardware, often
with no internet connection, and the customer has full administrator rights on
that machine. There is no licence server to phone. Anything the software stores
locally, the customer can in principle edit.

---

## Part one — the licence itself

A licence is a single file the supplier sends. It says who the customer is, how
many places they bought, which features they have, and when it expires. Funis
signs it cryptographically, so any edit to its contents breaks the signature.

> **The analogy.** A signed letter of authority. Anyone can read it, nobody can
> alter a word without the signature no longer matching, and MATOFX checks the
> signature every time it looks at the letter — not just the first time.

### Every licence belongs to one specific installation

This is the part that stops one licence quietly running two deployments. When
MATOFX first starts, it invents its own random identifier — something like
`inst_0V9H0FJ0RTTY` — and stores it in its database. The customer sends that
identifier to their supplier, and the licence is issued *for* it.

> **The analogy.** The house chooses its own address, then the deeds are drawn
> up naming that address. Copy the deeds to a different house and they plainly
> don't match, because the second house picked a different address of its own.

The direction matters more than it looks. Originally the supplier issued the
identifier *inside* the licence — which meant it identified the licence, not the
installation. Feed the same file to two deployments and both could honestly
claim the same identity. Inverting it fixed that.

### The two kinds of licence

| | **Concurrent Session (CSL)** | **Floating Access (FAL)** |
|---|---|---|
| A place is used… | when someone signs in | when an admin grants someone access |
| …and released | when they sign out or go idle | when an admin revokes it |
| Feels like | a shared pool of hire cars | named parking bays |

Both allow unlimited registered accounts. They differ only in what consumes a
place. A large team who each use MATOFX occasionally suits the first; a small
settled group suits the second.

### Changes take effect immediately

Licences run yearly, but they are also re-issued *during* a year to add people
or switch on a feature that has just been bought. Installing the new file
applies both halves at once, with no restart and no outage — extra places from
the next sign-in, new features on the next page load.

---

## Part two — when something is wrong

MATOFX draws a hard line between a licence that has *lapsed* and one that has
been *tampered with*, because they need opposite responses from the customer.

| Situation | What happens | What to do |
|---|---|---|
| Expired, still in grace | Everything works; a banner counts down | Renew before it runs out |
| Past the grace period | **Read-only** — view everything, submit nothing | Renew |
| File missing | Read-only | Put it back |
| File edited, or not from the supplier | Read-only, with a different message | *Restore* the real file — not a renewal |

Read-only rather than switching off is deliberate. A missed renewal must never
take a customer's simulation work offline — everything already produced stays
readable and downloadable. Jobs already running finish normally.

> **The analogy.** An expired library card. You can still sit and read
> everything on the shelves; you just can't borrow anything new until it's
> renewed.

### The fire-exit key

If every place is taken and a Super User signs in, MATOFX lets them in anyway.
Refusing would lock an administrator out of the site at precisely the moment the
licence needs fixing.

There is deliberately **no limit** on how often this happens — a limit would
lock out the very person it exists to admit. Three things keep it honest
instead: the session ends within 12 hours whatever the site's normal timeout,
administrators are emailed when it happens, and the compliance report shows
those admissions counted separately, so 11 people against a 10-place licence
reads as "one emergency admission" rather than looking like a breach.

> **The analogy.** The break-glass key by the fire exit. It always works, using
> it is always recorded, and the door doesn't stay propped open afterwards.

---

## Part three — proving nothing was doctored

MATOFX keeps a tamper-evident record of significant events. Each entry carries a
fingerprint of the entry before it, so the whole thing forms a chain.

> **The analogy.** A bound ledger where every page quotes the previous page's
> fingerprint in its header. Tear out or alter a page and every page after it
> stops matching — you'd have to rewrite the entire book from that point on.

### Why a signature alone isn't enough

Here is the uncomfortable part the design is built around. The customer's
machine holds the key that signs those records. Someone determined could
rewrite the whole book **and** re-sign every page, and it would verify
perfectly. A signature proves "a holder of this key wrote this". It does not
prove "this is what happened".

### The answer: the supplier remembers

Periodically the customer generates a **compliance report** and sends it to
their supplier. Funis records a small fingerprint of the record set as it stood
at that moment. The next report must *prove* that today's records are the same
history, only longer.

> **The analogy.** Your supplier photographs where your bookmark sat. Next time,
> you must demonstrate that everything before that bookmark is unchanged and the
> book has only grown. You can't produce that demonstration for a book you've
> rewritten — and you can't know what to fake, because the photograph is in
> their filing cabinet, not yours.

The clever part is that this stays small. The proof is a standard construction
(the same one web browsers use to audit certificate logs), and it grows
logarithmically: proving a 90,000-record history needs about seventeen
fingerprints, not 90,000. That is what makes one or two reports a year
practical.

### What a report contains — and doesn't

It carries the licence details, usage figures for the period (peak simultaneous
users, refused sign-ins, emergency admissions), the result of MATOFX's own
integrity check, and the proofs above.

It contains **no engineering data at all** — no project or job names, no inputs,
no results, no files. That is a commercial decision as much as a technical one:
a report a customer's legal team refuses to send is worth nothing.

Every report is produced as a pair — a signed data file to send, and a readable
page saying exactly the same thing — so an administrator can see what they are
about to disclose before disclosing it.

### Generating one

From **Admin → Licence & compliance** in the browser, or `matofx compliance
report` on the server. Both write to the same numbered sequence; there is one
history, not two.

Producing a report and never sending it is explicitly fine and causes no trouble
later. Each report proves it follows on from *every* earlier one, so the
supplier can check it against whichever they last received.

---

## Part four — what the supplier has to do

The evidence only works if Funis keeps its side. That is a real operational
duty, not a formality.

- **Keep the record of every report received**, per installation. Lose it and
  that customer's history becomes unprovable — permanently.
- **Actually record every report.** One verified but not filed is one that
  protects nobody.
- **Keep the signing keys offline**, with an unused spare. The spare is the only
  way to recover from a compromised key without shipping new software to every
  customer in the field.

That record — the ledger — is itself chained and signed, and carries a small
note recording how many entries it should have.

> **The analogy.** The chain catches someone altering a page in the middle. But
> tearing pages off the *end* leaves a perfectly valid shorter book — so the
> cover also says "this ledger has 300 entries". Removing that note is itself
> now treated as suspicious, because it is written automatically and cannot go
> missing by accident.

Backups verify the ledger before copying it, so a scheduled backup doubles as a
scheduled integrity check: the day it starts failing is the day an older intact
copy still exists.

---

## Part five — telling someone, while it matters

Administrators are emailed when a Super User is admitted over the limit, when a
sign-in is refused because every place was full, and when the licence enters its
grace period or expires.

Without these, the first sign that a customer had outgrown their licence would
be a compliance report — which at one or two a year could be months later.
Alerts are sent when the situation *changes*, never repeatedly, and switching
them off stops the email without stopping the event being recorded.

---

## What this cannot do — read this part twice

Being straight about the limits is what makes the rest credible. Overstating
them is the one way this design fails a customer's security review.

> **Never say "tamper-proof".** It is **tamper-evident** and
> **rollback-detectable**. Someone who extracts the signing key, rebuilds the
> records, *and* keeps every proof consistent across every future report defeats
> it. On hardware the customer controls, no purely local scheme can do better.

> **The analogy.** A wax seal, not a bank vault. It doesn't stop someone opening
> the letter — it makes it impossible to open one without anybody being able to
> tell.

Two more honest limits:

- **Detecting a cloned installation needs both copies to report.** It works by
  both claiming the same report number. If only the clone ever reports, that is
  indistinguishable from the original simply going quiet.
- **The evidence lives in the supplier's records**, not in the signature. That
  is where the weight sits, and why losing them matters so much.

### Why that's still commercially sound

Because enforcement was never meant to be cryptographic. It is **contractual** —
the same posture IBM and Oracle take with their own licence-audit tooling. The
vendor does not prevent under-reporting by force; it requires the reports to be
produced and kept, and a customer who cannot produce consistent evidence
defaults to the worst-case licence position.

That changes what the software has to be good at. Not catching a determined
attacker — impossible here — but making inconsistency impossible to avoid
without deliberate, sustained, provable lying, *while never accusing an honest
customer*.

> **The rule that shaped everything.** A false accusation is worse than a missed
> one. If the supplier cannot tell fraud from a customer who simply previewed a
> report and never sent it, then the accusation cannot be acted on at all — and
> every true one loses its force. So anything unverifiable is reported as
> **"cannot tell"**, and only a genuinely failed proof is ever reported as a
> contradiction.
