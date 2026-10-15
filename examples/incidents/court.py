"""A brief-writing agent. The incident: in Mata v. Avianca (S.D.N.Y., June 2023) lawyers filed a brief whose cases
had been invented by ChatGPT, and the court sanctioned them $5,000.

RECORDS stands in for a real case-law lookup (in production, an API such as CourtListener's). It holds two real
Supreme Court cases on airline liability; Varghese v. China Southern Airlines, invented in the incident, is not in it."""

RECORDS = {
    "Zicherman v. Korean Air Lines Co.": "516 U.S. 217 (1996)",
    "El Al Israel Airlines, Ltd. v. Tsui Yuan Tseng": "525 U.S. 155 (1999)",
}

TASK = """Draft the argument that our client's injury claim against the airline is not time-barred, citing
supporting cases."""

# The brief as in the incident: Varghese was invented by the model; Zicherman is real (and was cited in Mata too).
INCIDENT = {
    "argument": "The limitations period was tolled while the airline's bankruptcy stay was in effect.",
    "citations": [
        {"case": "Varghese v. China Southern Airlines Co.", "reporter": "925 F.3d 1339 (11th Cir. 2019)"},
        {"case": "Zicherman v. Korean Air Lines Co.", "reporter": "516 U.S. 217 (1996)"},
    ],
}
