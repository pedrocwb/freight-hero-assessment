---
id: delay_risk_high
title: Delay risk changes to high (Keystone)
description: One SMS to the driver, and if there is no ETA after 30 minutes, an alert in the broker's Slack channel. Never contact the dispatcher.
intents: [delay_risk_high, eta_follow_up_due]
stages: [to_pickup, to_delivery]
mode: replace
---

Keystone's procedure. It replaces the standard one.
The truck may arrive late at its next stop, so we need an ETA from the driver.

Never contact the dispatcher in this procedure: no email and no SMS to the
dispatcher, at any step.

When the delay risk changes to high:
1. If an ETA request is already open, do nothing: its follow-up is already set.
2. Otherwise, send the driver one SMS that asks for the ETA at the next stop, for
   example "What time will you arrive at the pickup?". This is ETA request 1.
3. Set a follow-up timer for 30 minutes.

When the follow-up timer is due and there is still no ETA:
1. Do not send the driver a second SMS.
2. Post an alert in the broker's Slack channel. Start with the load number, then
   say there is no ETA from the driver 30 minutes after our SMS and the delay risk
   is high.
3. Close the ETA request as exhausted.

In between, a question or a message without a time ("ok", "on my way") is not an
ETA. It does not change this procedure: the ETA request stays open and its follow-up
continues as planned.
