---
id: delay_risk_high
title: Delay risk changes to high
description: Ask the driver for the ETA by SMS, ask again after 30 minutes, then a Hero calls the driver.
intents: [delay_risk_high, eta_follow_up_due]
stages: [to_pickup, to_delivery]
---

The truck may arrive late at its next stop, so we need an ETA from the driver.
Count the ETA requests already sent: they are the attempts of the open ETA request.

When the delay risk changes to high:
1. If an ETA request is already open, do nothing: its follow-up is already set.
2. Otherwise, send the driver one SMS that asks for the ETA at the next stop, for
   example "What time will you arrive at the pickup?". This is ETA request 1.
3. Set a follow-up timer for 30 minutes.

When the follow-up timer is due and there is still no ETA:
- After 1 ETA request: send the driver a second SMS that asks for the ETA. This is
  ETA request 2. Set a follow-up timer for 30 minutes.
- After 2 ETA requests: do not text the driver again. Create an urgent Hero task to
  call the driver for the ETA. Then close the ETA request as exhausted.

In between, a question or a message without a time ("ok", "on my way") is not an
ETA. It does not change this procedure: the ETA request stays open and its follow-up
continues as planned.
