---
id: delay_risk_high
title: Delay risk changes to high
description: Ask the driver for an ETA and follow up until one arrives.
intents: [delay_risk_high, eta_follow_up_due]
stages: [to_pickup, to_delivery]
---

SMS the driver for the ETA. No ETA after 30 minutes: SMS again.
