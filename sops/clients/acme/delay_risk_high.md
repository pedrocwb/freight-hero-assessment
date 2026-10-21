---
id: delay_risk_high
title: "Late truck: asking the driver for an ETA (Acme)"
description: "Delay risk is high, or an ETA request got no ETA: ask every 20 minutes, up to 3 times."
intents: [delay_risk_high, eta_follow_up_due]
stages: [to_pickup, to_delivery]
mode: replace
---

Acme asks every 20 minutes, up to three times, before a person steps in. Count the ETA
requests already sent for the next stop (the ledger's attempts).

1. If an ETA request is already open for the next stop, do nothing. Its follow-up is
   already scheduled.
2. Otherwise, send the driver one SMS that asks for the arrival time at the next stop.
   This is ETA request 1. Wait 20 minutes for the answer.
3. If 20 minutes pass after request 1 with no ETA, send the driver a second SMS that
   asks for the ETA. This is ETA request 2. Wait 20 minutes.
4. If 20 minutes pass after request 2 with no ETA, send the driver a third SMS that asks
   for the ETA. This is ETA request 3. Wait 20 minutes.
5. If 20 minutes pass after request 3 with no ETA, do not text the driver again:
   a. email the dispatcher in the load's email thread: the driver has not answered three
      ETA requests; ask for the ETA at the next stop;
   b. open an urgent Hero task to call the driver for the ETA, listing the times of the
      three SMS;
   c. close the ETA request as exhausted.

An ETA is a clock time ("10:15", "ETA 2pm") or a time from now ("40 min out"). "Soon",
"on my way", "ok" or a question are not ETAs: they do not stop the requests.
