---
id: delay_risk_high
title: "Aurora: ten unanswered ETA requests, then a Hero"
description: "High delay risk: ten driver SMS, one minute apart, then a Hero after the final one-minute wait."
intents: [delay_risk_high, eta_follow_up_due]
stages: [to_pickup, to_delivery]
mode: replace
---

This is Aurora Transportes' deliberately aggressive demonstration procedure.
Write messages and Hero tasks in Brazilian Portuguese. Ask for the arrival time
at the current stop. Each request uses send_sms with requests="eta_request".

On delay_risk_high, if no ETA request is open, send request 1 immediately.
If a request is already open, keep its timer; do not send an extra SMS.

On eta_follow_up_due, read the open obligation's attempts:
- Attempts 1 through 9: send exactly ONE new ETA request to the driver. The new
  attempt is the previous count plus one. Wait one minute after the confirmed SMS.
- Attempts 10: the driver has had the final one-minute opportunity to respond.
  Send no more SMS. Create exactly ONE urgent Hero task to call the driver for the
  ETA, with the load, destination and the ten unanswered requests in its description.
  Then close_obligation(kind="eta_request", status="exhausted").

Use schedule_timer(kind="eta_request", due_in_minutes=1) after each SMS, including
request 10. In durable mode the delivery confirmation establishes this deadline
automatically. Never send all requests in one run, never use the standard 30-minute
cadence, never escalate immediately after sending SMS 10, and never send SMS 11.
No dispatcher email or broker Slack is needed for this procedure.

An ETA from the driver satisfies the request and cancels its timer. A message that
does not supply an ETA follows the applicable message SOP; it does not count as a
new contact attempt. The no-response demonstration sends no inbound driver messages.
