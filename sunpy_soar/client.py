import json
import pathlib

import astropy.table
import astropy.units as u
import requests
import sunpy.net.attrs as a
from sunpy import log
from sunpy.net.attr import and_
from sunpy.net.base_client import BaseClient, QueryResponseTable
from sunpy.time import parse_time

from sunpy_soar.attrs import SOOP, Product, walker

__all__ = ["SOARClient"]


class SOARClient(BaseClient):
    """
    Client to access the Solar Orbiter Archive (SOAR).
    """

    def search(self, *query, **kwargs):  # NOQA: ARG002
        query = and_(*query)
        queries = walker.create(query)

        results = []
        for query_parameters in queries:
            if "provider='SOAR'" in query_parameters:
                query_parameters.remove("provider='SOAR'")
            results.append(self._do_search(query_parameters))
        table = astropy.table.vstack(results)
        qrt = QueryResponseTable(table, client=self)
        qrt["Filesize"] = (qrt["Filesize"] * u.byte).to(u.Mbyte).round(3)
        qrt.hide_keys = ["Data item ID", "Filename"]
        return qrt

    @staticmethod
    def _construct_payload(query):
        """
        Construct search payload.

        Parameters
        ----------
        payload : dict[str]
            Payload to send to the TAP server.
        """
        # Construct ADQL query
        url_query = {}
        url_query["SELECT"] = "*"
        # Assume science data by default
        url_query["FROM"] = "v_sc_data_item"
        for q in query:
            if q.startswith("level") and q.split("=")[1][1:3] == "LL":
                # Low latency data
                url_query["FROM"] = "v_ll_data_item"

        url_query["WHERE"] = "+AND+".join(query)
        adql_query = "+".join([f"{item}+{url_query[item]}" for item in url_query])

        return {
            "REQUEST": "doQuery",
            "LANG": "ADQL",
            "FORMAT": "json",
            "QUERY": adql_query,
        }

    @staticmethod
    def _do_search(query):
        """
        Query the SOAR server with a single query.

        Parameters
        ----------
        query : list[str]
            List of query items.

        Returns
        -------
        astropy.table.QTable
            Query results.
        """
        tap_endpoint = "http://soar.esac.esa.int/soar-sl-tap/tap"
        payload = SOARClient._construct_payload(query)
        # Need to force requests to not form-encode the parameters
        payload = "&".join([f"{key}={val}" for key, val in payload.items()])
        # Get request info
        r = requests.get(f"{tap_endpoint}/sync", params=payload)
        log.debug(f"Sent query: {r.url}")
        r.raise_for_status()

        # Do some list/dict wrangling
        names = [m["name"] for m in r.json()["metadata"]]
        info = {name: [] for name in names}
        for entry in r.json()["data"]:
            for i, name in enumerate(names):
                info[name].append(entry[i])

        if len(info["begin_time"]):
            info["begin_time"] = parse_time(info["begin_time"]).iso
            info["end_time"] = parse_time(info["end_time"]).iso

        result_table = astropy.table.QTable(
            {
                "Instrument": info["instrument"],
                "Data product": info["descriptor"],
                "Level": info["level"],
                "Start time": info["begin_time"],
                "End time": info["end_time"],
                "Data item ID": info["data_item_id"],
                "Filename": info["filename"],
                "Filesize": info["filesize"],
                "SOOP Name": info["soop_name"],
            },
        )
        result_table.sort("Start time")
        return result_table

    def fetch(self, query_results, *, path, downloader, **kwargs):  # NOQA: ARG002
        """
        Queue a set of results to be downloaded.
        `sunpy.net.base_client.BaseClient` does the actual downloading, so we
        just have to queue up the ``downloader``.

        Parameters
        ----------
        query_results : sunpy.net.fido_factory.UnifiedResponse
            Results from a Fido search.
        path : str
            Path to download files to. Must be a format string with a ``file``
            field for the filename.
        downloader : parfive.Downloader
            Downloader instance used to download data.
        kwargs :
            Keyword arguments aren't used by this client.
        """
        base_url = "http://soar.esac.esa.int/soar-sl-tap/data?" "retrieval_type=LAST_PRODUCT"

        for row in query_results:
            url = base_url
            if row["Level"].startswith("LL"):
                url += "&product_type=LOW_LATENCY"
            else:
                url += "&product_type=SCIENCE"
            data_id = row["Data item ID"]
            url += f"&data_item_id={data_id}"
            filepath = str(path).format(file=row["Filename"], **row.response_block_map)
            log.debug(f"Queuing URL: {url}")
            downloader.enqueue_file(url, filename=filepath)

    @classmethod
    def _can_handle_query(cls, *query):
        """
        Check if this client can handle a given Fido query. Checks to see if a
        SOAR instrument or product is provided in the query.

        Returns
        -------
        bool
            True if this client can handle the given query.
        """
        required = {a.Time}
        optional = {a.Instrument, a.Level, a.Provider, Product, SOOP}
        if not cls.check_attr_types_in_query(query, required, optional):
            return False
        # check to make sure the instrument attr passed is one provided by the SOAR.
        # also check to make sure that the provider passed is the SOAR for which this client can handle.
        instr = [i[0].lower() for i in cls.register_values()[a.Instrument]]
        for x in query:
            if isinstance(x, a.Instrument) and str(x.value).lower() not in instr:
                return False
            if isinstance(x, a.Provider) and str(x.value).lower() != "soar":
                return False
        return True

    @classmethod
    def _attrs_module(cls):
        # Register SOAR specific attributes with Fido
        return "soar", "sunpy_soar.attrs"

    @classmethod
    def register_values(cls):
        return cls.load_dataset_values()

    @staticmethod
    def load_dataset_values():
        # Instrument attrs
        attrs_path = pathlib.Path(__file__).parent / "data" / "attrs.json"
        with attrs_path.open() as attrs_file:
            all_datasets = json.load(attrs_file)
        # Convert from dict to list of tuples
        all_datasets = list(all_datasets.items())

        # Instrument attrs
        instr_path = pathlib.Path(__file__).parent / "data" / "instrument_attrs.json"
        with instr_path.open() as instr_attrs_file:
            all_instr = json.load(instr_attrs_file)
        all_instr = list(all_instr.items())

        soop_path = pathlib.Path(__file__).parent / "data" / "soop_attrs.json"
        with soop_path.open() as soop_path_file:
            all_soops = json.load(soop_path_file)

        all_soops = list(all_soops.items())

        return {
            Product: all_datasets,
            a.Instrument: all_instr,
            SOOP: all_soops,
            a.Provider: [("SOAR", "Solar Orbiter Archive.")],
        }
