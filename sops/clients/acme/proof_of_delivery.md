---
id: proof_of_delivery
title: Proof of delivery (POD)
description: Arrival at the delivery stop, the POD due 120 minutes later, and the POD check.
intents: [status_update, pod_follow_up_due, document_processed]
stages: [to_delivery, at_delivery]
mode: replace
---

Acme needs a signed POD within 120 minutes of the truck's arrival at the delivery stop.

When the truck arrives at the delivery stop:
1. Set the load to at_delivery. The POD is due 120 minutes after the arrival (the `pod`
   obligation opens with that timer). Do not ask for it yet.

When 120 minutes have passed since the arrival and no POD has arrived (pod_follow_up_due
with no attempt yet):
2. Send the driver one SMS that asks for a photo of the signed POD, for example "Hi, Acme
   dispatch here. Please send a photo of the signed POD for this load." It requests the
   `pod`. This is the only reminder: wait 30 minutes.
3. If there is still no POD 30 minutes after the reminder, do not text the driver again.
   Open a Hero task to get the POD from the carrier. It is not urgent. Then close the
   POD request as exhausted.

When a POD arrives, act on the document check (the document_processed event), never on
the message that carried the file:
4. A clean POD (processed, every check ok): set the load to delivered and write a TMS
   note, for example "POD received, signed by the receiver at 09:42".
5. A POD with an exception, no signature or a value that does not match the load
   (needs_review): do not set the load to delivered. Open a Hero task with the values
   found and, for each value that does not match, the value on the POD and the value on
   the load. Send the driver nothing about it.
6. Never forward the POD to anyone.
