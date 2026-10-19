---
id: driver_departed
title: Driver departed a stop
description: The truck left a stop. Update the stage and write a TMS note.
intents: [driver_departed]
---

The driver tells us the truck has left a stop, for example "loaded, heading out" or
"just left the receiver".

1. Set the stage for the next part of the trip:
   - the next stop is a delivery: to_delivery;
   - the next stop is another pickup: to_pickup;
   - the truck left the last delivery: do not change the stage. The load becomes
     delivered when the POD is checked.
2. Write a TMS note with the stop and the local time, for example "Driver left the
   pickup at 10:05".
3. If an ETA request for the stop they left is still open, close it as satisfied:
   the truck got there.
4. Send no message, unless the text below adds a step for this client.
