"""Validity rules from the brief: UK workplace, or remote explicitly open to UK-based workers."""
from __future__ import annotations

import re

UK_MARKERS = re.compile(
    r"\b(united kingdom|uk|u\.k\.|great britain|britain|england|scotland|wales|northern ireland|"
    r"london|manchester|birmingham|leeds|glasgow|edinburgh|liverpool|bristol|sheffield|cardiff|"
    r"belfast|newcastle|nottingham|leicester|southampton|portsmouth|brighton|reading|oxford|"
    r"cambridge|coventry|bradford|hull|plymouth|stoke|wolverhampton|derby|swansea|aberdeen|"
    r"dundee|york|milton keynes|norwich|exeter|bath|luton|sunderland|middlesbrough|peterborough|"
    r"ipswich|slough|swindon|gloucester|cheltenham|chester|warrington|bolton|preston|blackpool|"
    r"guildford|watford|crawley|basingstoke|bournemouth|poole|colchester|chelmsford|maidstone|"
    r"canterbury|stockport|wigan|huddersfield|wakefield|doncaster|rotherham|lincoln|worcester|"
    r"hereford|telford|shrewsbury|inverness|stirling|perth|newport|wrexham|lisburn|derry|"
    r"greater london|greater manchester|west midlands|east midlands|yorkshire|lancashire|kent|"
    r"essex|surrey|sussex|hampshire|berkshire|buckinghamshire|hertfordshire|oxfordshire|"
    r"cambridgeshire|norfolk|suffolk|devon|cornwall|dorset|somerset|wiltshire|gloucestershire|"
    r"cheshire|merseyside|cumbria|northumberland|durham|tyne and wear|staffordshire|"
    r"warwickshire|leicestershire|nottinghamshire|derbyshire|lincolnshire|shropshire|"
    r"herefordshire|worcestershire|northamptonshire|bedfordshire|rutland|isle of wight|"
    r"lanarkshire|fife|lothian|ayrshire|highlands|county antrim|county down|gwynedd|powys|"
    r"west yorkshire|south yorkshire|north yorkshire|east sussex|west sussex|tyne)\b", re.I)

UK_POSTCODE = re.compile(r"\b[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}\b")

FOREIGN = re.compile(
    r"\b(united states|usa|u\.s\.|canada|australia|new zealand|india|ireland|dublin|germany|"
    r"france|spain|netherlands|belgium|poland|portugal|italy|switzerland|sweden|norway|denmark|"
    r"finland|austria|singapore|hong kong|uae|dubai|saudi|qatar|south africa|nigeria|kenya|"
    r"philippines|china|japan|brazil|mexico|emea|europe|worldwide|anywhere|overseas|"
    r"jersey|guernsey|isle of man|gibraltar)\b", re.I)
# Crown dependencies are not part of the UK; "New England"/"New South Wales" are not England/Wales.
FALSE_FRIENDS = re.compile(r"\b(new england|new south wales|london,? ontario|perth,? (wa|western australia))\b", re.I)

REMOTE = re.compile(r"\b(remote|work from home|wfh|home[- ]based|anywhere)\b", re.I)
UK_REMOTE_TEXT = re.compile(
    r"(uk[- ]based|based in the uk|within the uk|anywhere in the uk|across the uk|uk[- ]wide|"
    r"united kingdom|right to work in the uk|uk residents?|resident in the uk|uk only)", re.I)


def is_uk_listing(source: str, location: str | None, description: str | None) -> tuple[bool, str | None]:
    loc = (location or "").strip()
    if not loc:
        return False, "no_location"
    if FALSE_FRIENDS.search(loc):
        return False, "not_uk"
    uk_loc = bool(UK_MARKERS.search(loc) or UK_POSTCODE.search(loc.upper()))
    foreign_loc = bool(FOREIGN.search(loc))
    if foreign_loc and not re.search(r"\b(united kingdom|uk)\b", loc, re.I):
        return False, "not_uk"
    remote_only = bool(REMOTE.search(loc)) and not uk_loc
    if remote_only:
        # "Remote" alone is not enough: require the listing to say it's open to UK workers.
        if UK_REMOTE_TEXT.search(description or ""):
            return True, None
        return False, "remote_not_uk_explicit"
    if uk_loc:
        return True, None
    # Reed and Indeed UK show bare town names ("Hemel Hempstead") for UK-only inventory;
    # LinkedIn always appends the country, so an unrecognised LinkedIn location is not UK.
    if source in ("reed", "indeed"):
        return True, None
    return False, "not_uk"
