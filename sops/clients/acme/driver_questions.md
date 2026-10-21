---
id: driver_questions
title: Questions from the driver (Acme)
description: The driver or the dispatcher asks something about the load.
intents: [question]
mode: replace
---

1. Look for the answer in the load details: the stops and their addresses, the
   appointment times, the PO and reference numbers, the commodity and its weight, and the
   load instructions.
2. If the answer is there, reply on the channel the question came on, with the value
   copied exactly as it is written in the load details.
3. If the answer is not in the load details, do not guess and do not promise an answer.
   Open a Hero task with the question and who asked it.
4. Never share rates. Send no reply about the rate and open a Hero task instead.
5. A question is not an ETA. If an ETA request is open, leave it open, and do not ask for
   the ETA in the same reply.
