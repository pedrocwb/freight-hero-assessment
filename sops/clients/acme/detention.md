---
id: detention
title: "Detention: truck held at a facility"
description: The driver reports a wait of more than 2 hours at a shipper or a receiver.
intents: [detention_request]
stages: [at_pickup, at_delivery]
mode: replace
---

Acme gives every facility 2 hours of free time. Count the wait from the later of the
driver's check-in time and the appointment time; with no check-in time, from the
driver's first message sent from the facility.

A wait of 2 hours or less is not detention: write a TMS note with the facility and the
wait so far, and send nothing.

When the wait is more than 2 hours:
1. Write a TMS note with the facility, the check-in time, the appointment time and the
   wait so far.
2. Open a Hero task titled "Request detention at" followed by the facility name, with the
   same times. Mark it urgent only if the wait puts the load's next appointment at risk.
3. Send the driver one SMS: "Thanks, we noted your wait at the facility. Please have the
   in and out times written on the paperwork."
4. Never promise detention pay, never say whether detention will be paid, and never
   quote an amount.
5. Open one detention task per stop. If the driver writes again about the same wait,
   write a new TMS note with the new wait time, send no SMS and open no second task.
