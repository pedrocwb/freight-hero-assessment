---
id: driver_questions
title: Questions from the driver or other contacts
description: Answer from the load details, or from a facility note for a question about a stop's facility, otherwise create a Hero task. Never guess.
intents: [question]
---

A contact asks us something, for example "What's the PO number?" or "Where do I
check in?".

1. Look for the answer in the load details: the stops, their addresses and
   appointments, the commodity and the load instructions. The PO number is in the
   load instructions, for example "PO ... required at gate".
2. If the answer is there, reply to the person who asked, on the channel they used,
   with that answer copied exactly. For example, reply "The PO number is" followed
   by the number written in the instructions.
3. If the load details do not answer a question about a stop's facility (where to
   check in, the hours, what the gate asks for, how to get in), look in the facility
   notes for that stop: the notes in the prompt, or `recall_facility`. If a note
   answers it, reply with the note copied exactly, and say it is what we were told on
   an earlier load, for example "On an earlier load we were told: check in at gate
   4". Use only what the note says, and never a note about rates, pay or contacts
   outside the load. Create no Hero task for a question a note answers.
4. If the answer is in neither, do not guess and do not reply with a promise. Create
   a Hero task with the question, who asked it, and what they need.
5. Never answer questions about rates, pay or the rate confirmation. Create a Hero
   task instead.
6. A question is not an ETA. If an ETA request is open, leave it and its follow-up
   as they are, and do not ask for the ETA again in your reply.
