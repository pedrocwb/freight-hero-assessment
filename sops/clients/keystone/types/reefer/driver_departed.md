---
id: driver_departed
mode: extend
---

Keystone refrigerated (reefer) loads add one step.

When the driver leaves the pickup (not a delivery stop):
1. Ask the driver for the trailer temperature and the seal number, both in one SMS,
   for example "Please send the trailer temperature and the seal number."
2. This is the only SMS to the driver for this event.
3. When the driver answers, write the temperature and the seal number in a TMS note.
