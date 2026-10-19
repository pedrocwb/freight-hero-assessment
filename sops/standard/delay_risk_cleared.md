---
id: delay_risk_cleared
title: Delay risk is back to normal
description: The delay risk dropped from high. Cancel the open ETA request quietly and send nothing.
intents: [delay_risk_low]
---

The delay risk was high and is now lower. The truck is expected on time again, so we
no longer chase the driver for an ETA.

1. If an ETA request is open, close it as cancelled. This also stops its follow-up.
2. If no ETA request is open, do nothing.
3. Send no message to anyone: no SMS, no email, no Slack post.
4. Do not create a Hero task.

If a Hero task about the ETA was already created, leave it as it is.
