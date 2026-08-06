"""biocOnIce: Bioconductor annotation as Apache Iceberg tables."""

import os
from pathlib import Path

from pyiceberg.catalog import load_catalog


def catalog():
    """The biocOnIce catalog.

    Defaults to a local sqlite-backed warehouse (`./warehouse`, override with
    `BIOCONICE_WAREHOUSE`) so nothing needs cloud credentials. Set
    `BIOCONICE_URI` to talk to a REST catalog (icegate) instead, with
    `BIOCONICE_WAREHOUSE` as the catalog name and `BIOCONICE_TOKEN` as the key.
    """
    uri = os.environ.get("BIOCONICE_URI")
    if uri:
        props = {"type": "rest", "uri": uri,
                 "warehouse": os.environ.get("BIOCONICE_WAREHOUSE", "biocOnIce")}
        token = os.environ.get("BIOCONICE_TOKEN")
        if token:
            props["token"] = token
        return load_catalog("bioconice", **props)

    warehouse = Path(os.environ.get("BIOCONICE_WAREHOUSE", "warehouse")).absolute()
    warehouse.mkdir(parents=True, exist_ok=True)
    return load_catalog("bioconice", type="sql",
                        uri=f"sqlite:///{warehouse}/catalog.db",
                        warehouse=warehouse.as_uri())
