---
id: delay_risk_high
title: Delay risk changes to high (Northline)
description: Two SMS to the driver, then email the dispatcher.
intents: [delay_risk_high, eta_follow_up_due]
stages: [to_pickup, to_delivery]
mode: replace
---

SMS the driver twice, then email the dispatcher and create a Hero task.
