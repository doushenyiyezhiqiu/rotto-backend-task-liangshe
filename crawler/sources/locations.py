"""Search shards. Every board caps how deep one search can paginate (Indeed and LinkedIn
stop around 1,000 results), so coverage comes from many narrower searches."""

UK_LOCATIONS = [
    "London", "Manchester", "Birmingham", "Leeds", "Glasgow", "Edinburgh", "Liverpool",
    "Bristol", "Sheffield", "Newcastle upon Tyne", "Nottingham", "Leicester", "Cardiff",
    "Belfast", "Southampton", "Reading", "Milton Keynes", "Cambridge", "Oxford", "Brighton",
    "Coventry", "Bradford", "Kingston upon Hull", "Plymouth", "Stoke-on-Trent", "Derby",
    "Wolverhampton", "Aberdeen", "Swansea", "Norwich", "Exeter", "York", "Peterborough",
    "Portsmouth", "Ipswich", "Luton", "Northampton", "Preston", "Bournemouth", "Swindon",
    "Gloucester", "Chelmsford", "Maidstone", "Guildford", "Warrington", "Sunderland",
    "Middlesbrough", "Lincoln", "Dundee", "Inverness",
]

# Broad keywords used to split very large locations (London, Manchester...) into searches
# that each stay under the pagination cap.
KEYWORDS = [
    "", "engineer", "developer", "manager", "assistant", "nurse", "care", "teacher",
    "driver", "sales", "accountant", "administrator", "warehouse", "analyst", "support",
    "customer service", "chef", "cleaner", "electrician", "recruitment", "marketing",
    "finance", "hr", "data", "project", "operations", "technician", "consultant", "graduate",
    "remote",
]

BIG_LOCATIONS = ["London", "Manchester", "Birmingham", "Leeds", "Glasgow", "Bristol"]


def slug(s: str) -> str:
    import re
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
