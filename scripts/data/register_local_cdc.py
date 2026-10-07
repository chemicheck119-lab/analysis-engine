"""Register the fixed loopback-only, fixture-only Debezium connector."""

import json
from pathlib import Path
from urllib.request import Request, urlopen

if __name__ == "__main__":
    config = json.loads(
        (Path(__file__).resolve().parents[2] / "local/cdc/connector.json").read_text()
    )
    request = Request(
        "http://127.0.0.1:58083/connectors/chemicheck-approved-local/config",
        data=json.dumps(config["config"]).encode(),
        method="PUT",
        headers={"Content-Type": "application/json"},
    )
    with urlopen(request, timeout=30) as response:
        print(json.dumps({"registered": response.status in (200, 201)}))
