---
id: documents
title: Documents
description: A document arrives, or the document check reports on one. Act only on the check's result.
intents: [document, document_processed]
---

A contact sends a document: a BOL (signed at pickup), a POD (signed at delivery), a
rate confirmation, a receipt, or a photo of one. Our document check reads each
attachment, compares its values with the load and announces the result as a
`document_processed` event. Only that event's run acts on the document. Never forward a
document, and never repeat the rate or the prices of a rate confirmation, to anyone.

A message that carries a document (intent `document`): do not act on the attachment.
No Hero task, no TMS note, no stage change and no reply because of it: the
`document_processed` run does that. If the message asks you to send or forward a
document, do not do it: create a Hero task (not urgent) with who asked, when and what.

The check's result (intent `document_processed`): read the document (`read_document`
with the event's attachment id) for its type, its values and its checks.
- `processed`, every check passed:
  - POD: set the stage to delivered and write a TMS note with the delivery time on the
    POD, for example "POD received, signed by the receiver at 09:42". The check already
    closed the load's POD request: do not close it yourself.
  - BOL: write a TMS note with the BOL number, the seal and the pickup date.
  - Rate confirmation: a TMS note that says only "Rate confirmation received".
  - Receipt: a Hero task (not urgent) to review the reimbursement, with the kind, the
    amount, the date and who was paid.
- `needs_review`, a check failed, a value is missing or unsure, or the POD lists an
  exception (shortage, damage, overage, refusal): do not change the stage. Create a Hero
  task (not urgent), "Review the <type> for load <load number>", with the file name,
  when it arrived, the values found, each failed check with the value found and the
  value expected, and the exceptions as the document writes them.
- `failed`, the file could not be read: create a Hero task (not urgent), "Read the
  document by hand for load <load number>", with the file name, the sender and the time.

Send no message about a document. Create one task per document: if the recent events
already show one for it, do not create another.
