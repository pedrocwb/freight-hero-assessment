---
id: eta_deferred
title: ETA promised for later
description: The contact promises the answer within some minutes. Accept one wait of at most 15 minutes and send nothing.
intents: [eta_deferred]
---

Sometimes the driver or the dispatcher promises the ETA a little later, for example
"I'll tell you in 5 min" or "give me 10 minutes".

1. If no ETA request is open, do nothing.
2. Each ETA request accepts one wait. If this one already used it (deferrals used
   is 1), do not accept another: send nothing and change nothing.
3. If they give no number of minutes ("later", "let me check"), do not wait for them:
   send nothing and change nothing. The follow-up continues as planned.
4. Otherwise, defer the ETA request by the minutes they promised, at most 15:
   "in 5 min" is 5 minutes, "in 30 min" is 15 minutes. The follow-up moves to just
   after that time. Do not set another follow-up timer.
5. Send nothing. A wait is not an ETA request and does not count as one.

If the follow-up comes due and there is still no ETA, continue the delay risk
procedure as usual.
