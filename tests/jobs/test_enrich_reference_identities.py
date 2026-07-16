from __future__ import annotations

import json
from pathlib import Path

from endoscan_jobs.jobs.enrich_reference_identities import build_artifact

VALID_KEY = "QAIPRVGONGVQAS-DUXPYHPUSA-N"
INVALID_KEY = "NOT-AN-INCHIKEY"


class FakePubChemClient:
    requests_per_second = 3.0

    def __init__(self) -> None:
        self.request_count = 0
        self.paths: list[str] = []

    def get_json(self, path: str) -> dict:
        self.request_count += 1
        self.paths.append(path)
        if "/property/" in path:
            return {
                "PropertyTable": {
                    "Properties": [
                        {
                            "CID": 689043,
                            "InChIKey": VALID_KEY,
                            "Title": "Caffeic Acid",
                            "IUPACName": "(E)-3-(3,4-dihydroxyphenyl)prop-2-enoic acid",
                            "ConnectivitySMILES": "C1=CC(=C(C=C1C=CC(=O)O)O)O",
                            "SMILES": "C1=CC(=C(C=C1/C=C/C(=O)O)O)O",
                        }
                    ]
                }
            }
        return {
            "InformationList": {
                "Information": [{"CID": 689043, "Synonym": ["Caffeic Acid", "Caffeic acid"]}]
            }
        }


def test_batch_pubchem_enrichment_records_resolved_and_invalid_identities(tmp_path: Path) -> None:
    explore = tmp_path / "models/ER/explore"
    explore.mkdir(parents=True)
    (explore / "umap.json").write_text(
        json.dumps({"points": [{"compound_id": VALID_KEY}, {"compound_id": INVALID_KEY}]}),
        encoding="utf-8",
    )
    client = FakePubChemClient()
    artifact = build_artifact(tmp_path, client, artifact_version="test-v1")

    assert artifact["statistics"]["total_unique_inchikeys"] == 2
    assert artifact["statistics"]["resolved"] == 1
    assert artifact["statistics"]["failed_lookup_categories"]["invalid_inchikey"] == 1
    resolved = next(item for item in artifact["identities"] if item["inchikey"] == VALID_KEY)
    assert resolved["preferred_name"] == "Caffeic Acid"
    assert resolved["pubchem_cid"] == 689043
    assert resolved["synonyms"] == ["Caffeic Acid"]
    assert len(client.paths) == 2
