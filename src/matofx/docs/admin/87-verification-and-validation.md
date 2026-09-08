---
title: Verification and validation (V&V)
audience: admin
capability: null
slug: admin-verification-and-validation
summary: Turning on V&V, granting the create and curator capabilities, why you need at least two curators, the direct-approve option, and notifications.
---

# Verification and validation (V&V)

**Verification and validation** lets your users record evidence-backed claims that
a tool — or a specific result — has been *checked*, and turns approved claims into
the **V&V badges** everyone sees. The day-to-day authoring, reviewing and record
lifecycle are covered in the user guide (*Verification and validation (V&V)*); this
chapter covers the parts **you** control as an administrator.

## Turning V&V on

V&V is a **feature switch** in **General Settings**. When it's off, the **V&V
records** page and the **New record** button are absent, and badges don't appear on
tools and results. Turn it on to make the whole surface available (see *Site settings
and branding*).

## Who can author and who can review

V&V uses the two-tier model described in *People and access*: a base right to
create, and a separate **capability** to sign off.

- **V&V create** — lets a user author a **draft** record. Give this to the
  engineers who run the checks.
- **V&V curator** — lets a user **submit, review, approve, reject and retire**
  records, and use **Migrate to new version**. Give this to the people responsible
  for signing off quality claims.

Grant both from a user's page (*People and access*). Anyone signed in can already
*browse* the register and see badges without either capability.

## You need at least two curators

A record can only be approved by a curator **other than** the one who submitted it
— the *directed-approver* rule, which forbids self-approval. If only one person
holds the V&V curator capability, they can author records but nobody can approve
them. **Grant the curator capability to at least two people** so reviews can
actually complete. Records a curator authored never appear in their own review
queue, and the Approve/Reject buttons are disabled on their own submissions.

## Review-required vs. direct approval

By default a submitted record waits in **Pending review** until a *different*
curator decides it. Your site can instead be configured to **record approvals
directly**, so submitting a record approves it in one step (no separate reviewer).
The default — requiring a reviewer — is the stronger governance posture; use direct
approval only where a second-person review isn't warranted.

## Notifications

When a record is submitted for review, the nominated curator can be **emailed** that
a decision is waiting on them, alongside the other approval-request notifications.
This is governed by the approval-request email settings — see *Approvals* and *Site
settings and branding* for the master switch and per-type toggles.

## Related

- *Verification and validation (V&V)* (user guide) — the record kinds, evidence,
  the full draft → review → approved → retired lifecycle, inheritance across
  byte-identical tool bundles, and migrating records to a new tool version.
- *Approvals* — the wider sign-off model and approval-request emails.
- *People and access* — granting the create and curator capabilities.
