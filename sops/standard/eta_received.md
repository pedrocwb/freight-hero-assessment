---
id: eta_received
title: ETA received
description: A contact gives an arrival time. Close the ETA request and write the ETA in the TMS. A vague answer is not an ETA.
intents: [eta_provided]
---

An ETA is the time the truck will arrive at the next stop, from the driver or the
dispatcher.

What counts as an ETA:
- A clock time: "9:40", "ETA 10am", "be there around 7:50".
- A duration: "in 40 min", "30 mins out", "about an hour". Add it to the time of the
  message: a message sent at 07:20 that says "30 mins out" means 07:50.

What is not an ETA: "soon", "on my way", "almost there", "ok", "thanks". They give
no time.

When the message gives an ETA:
1. If an ETA request is open, close it as satisfied. This also stops its follow-up.
2. Write a TMS note with the ETA as a local clock time, the stop and who sent it, for
   example "ETA 07:50 at the pickup (driver, by SMS at 07:20)".
3. Send no reply, and do not ask for the ETA again.

When the message gives no ETA:
1. Send nothing and write nothing.
2. Leave the ETA request open: its follow-up continues as planned.
