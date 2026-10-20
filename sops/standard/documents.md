---
id: documents
title: Documents
description: A BOL, POD, rate confirmation, receipt or photo arrives. For now a Hero reviews every document.
intents: [document, document_processed]
---

A contact sends a document: a BOL (signed at pickup), a POD (signed at delivery), a
rate confirmation, a receipt, or a photo of one.

For now, a person checks every document:
1. Create a Hero task to review the document. It is not urgent.
2. In the description, write who sent it, when (local time), the file name, and what
   the message says it is, if it says.
3. Ask the Hero to confirm the type of document, check its key values (for example
   the date, the signatures and the reference numbers) and update the load.
4. Send no reply to the sender.
5. Never forward a rate confirmation, and never repeat its prices, to anyone.
