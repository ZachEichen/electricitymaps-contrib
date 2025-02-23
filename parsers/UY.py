#!/usr/bin/python3
import re
from datetime import datetime, timedelta
from logging import Logger, getLogger
from typing import Callable, List, Optional

import arrow
import dateutil
from bs4 import BeautifulSoup
from requests import Session

from parsers.lib.exceptions import ParserException

TIME_ZONE = "America/Montevideo"

# maps the xml keys from the website to names used by parser system
MAP_GENERATION = {
    # short for rio negro, main hydro power plant
    "hydro": "r",
    "wind": "eolica",
    "solar": "fotovoltaica",
    "biomass": "biomasa",
    # all thermal, i.e. miscilaneous oil + natural gas production
    "unknown": "termica",
    "trade": "intercambios",
    "demand": "demanda",
    "salto_grande_agg": "comprassgu",
}

AVALIABLE_KEYS = ["hydro", "wind", "solar", "biomass", "unknown"]

UTE_URL = url = "https://ute.com.uy/energia-generada-intercambios-demanda"

SALTO_GRANDE_URL = "http://www.cammesa.com/uflujpot.nsf/FlujoW?OpenAgent&Tensiones y Flujos de Potencia&"


def get_salto_grande(session: Session, targ_time: datetime) -> float:
    """Finds the current generation from the Salto Grande Dam that is allocated to Uruguay."""

    # Shift hours around to match up with UY's data source as suggested by
    # https://github.com/electricitymaps/electricitymaps-contrib/pull/4840#discussion_r1045617671
    targ_time = targ_time.floor("hour") - timedelta(hours=1)

    lookup_time: str = targ_time.floor("hour").format("DD/MM/YYYY HH:mm")

    url = SALTO_GRANDE_URL + lookup_time
    response = session.get(url)
    soup = BeautifulSoup(response.text, "html.parser")

    tie = soup.find("div", style="position:absolute; top:143; left:597")
    if not tie:
        raise ParserException(
            parser="UY",
            message=f"Error parsing XML data string from page from secondary source; url={SALTO_GRANDE_URL}",
        )

    generation = float(tie.text)

    return generation


def parse_num(num_str: str) -> float:
    """
    given a string containing a decimal number in
    south american format, e.g. '23.456,45' using ',' as a decimal
    and '.' as a separator, convert to the corresponding float
    :param num_str: string containing a number south american format,
        e.g. '23.456,45' using ',' as a decimal
    :returns: a float with the value denoted by num_str
    """
    return float(num_str.strip().replace(".", "").replace(",", "."))


def correct_for_salto_grande(entry, session: Session) -> dict:
    """
    corrects a single entry parsed from the UTE website using salto_grande data
    Switched this to only operate on a single entry or list of entries so
    fewer requests are needed
    """
    # our current source of hydro electric power only considers power generated
    # at the rio negro power generation plant. There is an additional source
    # which is power generated at the Salto Grande dam. This is in contrast to the
    # comprassgu field which is a total between exchange done at the Salto Grande site
    # and the amount of energy bought from the argentinian side of the dam.
    # Therefore, we use the workaround described in the below commit to get the actual generation
    # in a way that is compatible with the way we represent trade with argentina.
    # https://github.com/electricitymaps/electricitymaps-contrib/issues/1325#issuecomment-380453296
    salto_grande = get_salto_grande(session, entry["time"])
    entry["hydro"] = entry["hydro"] + salto_grande
    return entry


def parse_page(session: Session):
    """
    Queries the url in UTE_URL, and parses hourly production and trade data
    and retruns the results as a list of dictionary objects.
    :param session: requests.session object that will be used to make API requests
    :returns: a dictionary mapping data items to their values (in MW) for the past hour:
        ['hydro', 'wind', 'solar', 'biomass', 'unknown', 'trade', 'demand', 'salto_grande_agg']
        where 'trade' is the total average imported power in MW (can be negative)
        and 'salto_grande_agg' is the total power imported and produced at the salto grande site
    """
    # load page
    resp = session.get(UTE_URL)

    # get the encoded XML with all the data we want
    soup = BeautifulSoup(resp.text, "html.parser")
    xml_tag = soup.find(id="valoresParaGraficar")
    if not xml_tag:
        raise ParserException(
            parser="UY", message="Error parsing XML data string from page"
        )
    xml_string = f"<{xml_tag.contents[0]}>{xml_tag.contents[1]}"

    # Use beautiful soup  to actually parse that XML string, and get information
    xml_doc = BeautifulSoup(xml_string, "lxml")

    # Data is encoded hourly... get that subsection of the document
    # then get the data for each encoded hour, as a json-like object

    hourly = xml_doc.find("fuentesporhora")
    if not hourly:
        raise ParserException(
            parser="UY", message="Error parsing hourly data from xml string"
        )
    hour_recs = []
    for hour in hourly.find_all("nodo"):
        # get generation data
        datum = {
            key: parse_num(hour.find(spanish).contents[0])
            for key, spanish in MAP_GENERATION.items()
        }
        # some values can sometimes return -0.1 at night, round up to 0
        for key, val in datum.items():
            if key not in ["trade", "demand"]:
                datum[key] = max(val, 0)

        # ingest date field
        datefield = hour.find("hora").contents[0]
        datestr = re.findall("\d\d/\d\d/\d\d\d\d \d+:\d\d", str(datefield))[0]
        date = arrow.get(datestr, "DD/MM/YYYY h:mm").replace(
            tzinfo=dateutil.tz.gettz(TIME_ZONE)
        )
        datum["time"] = date

        # add to list
        hour_recs.append(datum)
    return hour_recs


def get_entry_list(
    session: Session, make_output: Callable[[dict], dict], correct_hydro=True
) -> List[dict]:
    """
    Creates list of return datapoints given make_output function

    The function will first fetch data for all avaliable times,
    then use `make_output` to format the data points before returning
    a 24 hr history of data entries
    """
    # handle datetime here, so we only have to do it once
    # get data from webpage
    raw_entries = parse_page(session)

    entries = []
    for raw_entry in raw_entries:
        if correct_hydro:
            # https://github.com/tmrowco/electricitymap/issues/1325#issuecomment-380453296
            raw_entry = correct_for_salto_grande(raw_entry, session)
        entries.append(make_output(raw_entry))

    return entries


def fetch_consumption(
    zone_key: str = "UY",
    session: Session = Session(),
    target_datetime: Optional[datetime] = None,
    logger: Logger = getLogger(__name__),
) -> List[dict]:
    """
    Takes a zone key, session and optional datetime and returns the consumption
    for the UY reigon for the past 24 hours

    This parser is not at this time able to parse dates in the past. Therefore if
    a datetime is passed, it will throw a parser exception.
    :param zone_key: the key of the desired zone. Should only be UY
    :param session: a `requests.Session` object that will be used to make API requests
    :praram target_datetime: an optional datetime object that is the desired time.
        This parser is not at this time able to parse dates in the past. Therefore if
        a datetime is passed, it will throw a parser exception.
    :param logger: a logger if needed
    :returns: a list of dictionaries corresponding to the consumption data for the last 24 hrs
        each entry  has the following keys
            * "zoneKey":  the key passed to the parser
            * "datetime":  a datetime object containing the datetime that the entry applies to
            * "consumption":  a the average consumption for that hour, in MW
            * "source":  the source of the information
    """

    if target_datetime is not None:
        raise ParserException(
            parser="UY", message="This parser is unnable to parse dates in the past"
        )

    # helper func to format data output
    def make_datum(entry):
        return {
            "zoneKey": zone_key,
            "datetime": entry["time"].datetime,
            "consumption": entry["demand"],
            "source": "ute.com.uy",
        }

    # then delegate all the actual work to get_entry_list
    # correct_hydro is set to false, becuase we dont use hydro here
    # and correcting for hydro takes quite a bit of time (lots of API calls)
    return get_entry_list(session, make_datum, correct_hydro=False)


def fetch_production(
    zone_key: str = "UY",
    session: Session = Session(),
    target_datetime: Optional[datetime] = None,
    logger: Logger = getLogger(__name__),
) -> List[dict]:
    """
    Takes a zone key, session and optional datetime and returns the production data
    for the UY reigon for the past 24 hours

    This parser is not at this time able to parse dates in the past. Therefore if
    a datetime is passed, it will throw a parser exception.
    :param zone_key: the key of the desired zone. Should only be UY
    :param session: a `requests.Session` object that will be used to make API requests
    :praram target_datetime: an optional datetime object that is the desired time.
        This parser is not at this time able to parse dates in the past. Therefore if
        a datetime is passed, it will throw a parser exception.
    :param logger: a logger if needed
    :returns: a list of dictionaries corresponding to the production data for the last 24 hrs
        each entry  has the following keys
            * "zoneKey":  the key passed to the parser
            * "datetime":  a datetime object containing the datetime that the entry applies to
            * "production":  a dictionary indicating the production in MW for the  categories
                    "hydro", "wind", "solar", "biomass", and "unknown"
            * "source":  the source of the information
    """

    if target_datetime is not None:
        raise ParserException(
            parser="UY", message="This parser is unnable to parse dates in the past"
        )

    # make a helper function to create output format
    def make_datum(entry):
        return {
            "zoneKey": zone_key,
            "datetime": entry["time"].datetime,
            "production": {key: entry[key] for key in AVALIABLE_KEYS},
            "source": "ute.com.uy",
        }

    # then delegate all the actual work to get_entry_list
    return get_entry_list(session, make_datum, correct_hydro=True)


def fetch_exchange(
    zone_key1: str = "UY",
    zone_key2: str = "BR-S",
    session: Session = Session(),
    target_datetime: Optional[datetime] = None,
    logger: Logger = getLogger(__name__),
) -> List[dict]:
    """Requests the last known power exchange (in MW) between two countries."""

    if target_datetime is not None:
        raise ParserException(
            parser="UY", message="This parser is unnable to parse dates in the past"
        )

    # set comparison
    if (zone_key1, zone_key2) != ("BR", "UY"):
        print(zone_key1, zone_key2)
        raise ParserException(
            parser="UY",
            message="This parser is unnable to parse information on that feature",
        )

    # make a helper function to create output format
    def make_datum(entry):
        return {
            "sortedZoneKeys": "->".join([zone_key1, zone_key2]),
            "datetime": entry["time"].datetime,
            "netFlow": entry["trade"],
            "source": "ute.com.uy",
        }

    # then delegate all the actual work to get_entry_list
    # correct_hydro is set to false, becuase we dont use hydro here
    # and correcting for hydro takes quite a bit of time (lots of API calls)
    return get_entry_list(session, make_datum, correct_hydro=False)


if __name__ == "__main__":
    session = Session()
    print("fetch_production() ->")
    print(fetch_production(session=session))
    print("fetch_consumption() ->")
    print(fetch_consumption(session=session))
    print("fetch_exchange(BR, UY) ->")
    print(fetch_exchange("BR", "UY", session=session))

    print("fetch_exchange(BR, UY) ->")
    print(fetch_exchange(zone_key1="BR", zone_key2="UY", session=session))
