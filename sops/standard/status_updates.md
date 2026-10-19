---
id: status_updates
title: Status updates
description: News about the truck or the load. Write a TMS note when it matters, otherwise do nothing.
intents: [status_update]
---

A status update is news that is not an ETA and not a departure, for example "at the
gate", "stuck in traffic", "waiting for paperwork" or a trailer temperature.

The update matters when the broker would want it in their records:
- The truck is late, stopped or waiting: traffic, weather, a long wait at the dock.
- The driver arrived at a stop.
- A trailer temperature or a seal number.
- A problem with the truck or the load.

When the update matters:
1. Write a TMS note with the news and the local time, for example "Driver reports
   heavy traffic before the pickup, 07:20".
2. If the driver arrived at a stop, also set the stage to at_pickup or at_delivery,
   and close an open ETA request as satisfied: the truck is there.
3. If the problem stops the truck or the load (a breakdown, an accident, a refused or
   damaged load), also create an urgent Hero task.

When the update does not matter (small talk, nothing new), do nothing.
Send no reply in either case. An update without a time is not an ETA: leave an open
ETA request and its follow-up as they are.
