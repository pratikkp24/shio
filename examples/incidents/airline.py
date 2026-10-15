"""An airline's support desk. Two real incidents: in February 2024 a Canadian tribunal held Air Canada to a
bereavement-refund policy its website chatbot had invented (Moffatt v. Air Canada, 2024 BCCRT 149), and in April
2025 Cursor's AI support agent told users about a one-device login policy that did not exist.

The policy below is this example's own, written the way the real one worked: bereavement fares are not refunded
after travel."""

POLICY = """Bereavement travel
1. Bereavement fares are available for travel booked because of the death of an immediate family member.
2. The bereavement fare must be requested before travel. It cannot be applied to tickets that have already been used.
3. Requests are made by calling reservations with the passenger's details and the name of the deceased.
Refunds
4. Unused non-refundable fares may be exchanged for travel credit within one year of issue."""

QUESTION = """My grandmother passed away. I flew to Toronto yesterday on a full-fare ticket. Can I still get the
bereavement fare and a refund of the difference?"""

# What the chatbot said in the incident, as a structured answer: the reply and the clause it says it rests on.
INCIDENT = {"reply": "Yes. You can apply for the bereavement fare within 90 days of the ticket being issued, and "
                     "we will refund the difference.",
            "quote": "Bereavement fares may be requested within 90 days of the date the ticket was issued."}
