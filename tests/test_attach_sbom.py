"""attach_sbom: the documents it builds, and the whole flow against an in-memory registry."""

from __future__ import annotations

import json
from typing import Any

import pytest

import attach_sbom as a

IMAGE = {"mediaType": a.OCI_MANIFEST, "digest": "sha256:" + "1" * 64, "size": 2383, "platform": {"architecture": "amd64", "os": "linux"}}
OTHER = {"mediaType": a.OCI_MANIFEST, "digest": "sha256:" + "2" * 64, "size": 2383, "platform": {"architecture": "arm64", "os": "linux"}}
PROVENANCE = {"mediaType": a.IN_TOTO, "digest": "sha256:" + "3" * 64, "size": 40480, "annotations": {a.PREDICATE_ANNOTATION: "https://slsa.dev/provenance/v1"}}
OLD_SBOM = {"mediaType": a.IN_TOTO, "digest": "sha256:" + "4" * 64, "size": 86953211, "annotations": {a.PREDICATE_ANNOTATION: a.SPDX_PREDICATE}}
REF = "ghcr.io/linuxserver-playground/build/webtop:amd64-abc1234-40199"


def attestation_descriptor(digest: str, image: dict) -> dict:
    return {
        "mediaType": a.OCI_MANIFEST,
        "digest": digest,
        "size": 1112,
        "annotations": {a.REFERENCE_DIGEST: image["digest"], a.REFERENCE_TYPE: "attestation-manifest"},
        "platform": {"architecture": "unknown", "os": "unknown"},
    }


def test_parse_ref() -> None:
    assert a.parse_ref(REF) == ("ghcr.io", "linuxserver-playground/build/webtop", "amd64-abc1234-40199")
    for bad in ("webtop:latest", "ghcr.io/org/name", "ghcr.io/org/name@sha256:abc"):
        with pytest.raises(ValueError):
            a.parse_ref(bad)


def test_statement_matches_buildkit_shape() -> None:
    doc = a.statement({"spdxVersion": "SPDX-2.3"}, REF, IMAGE)
    assert doc["_type"] == a.STATEMENT and doc["predicateType"] == a.SPDX_PREDICATE
    assert doc["subject"] == [
        {
            "name": "pkg:docker/ghcr.io/linuxserver-playground/build/webtop@amd64-abc1234-40199?platform=linux%2Famd64",
            "digest": {"sha256": "1" * 64},
        }
    ]
    assert doc["predicate"] == {"spdxVersion": "SPDX-2.3"}


def test_attestation_manifest_replaces_spdx_and_keeps_provenance() -> None:
    existing = {"artifactType": a.ATTESTATION_ARTIFACT, "config": a.EMPTY_CONFIG, "layers": [OLD_SBOM, PROVENANCE]}
    layer = {"mediaType": a.IN_TOTO, "digest": "sha256:" + "5" * 64, "size": 6000000, "annotations": {a.PREDICATE_ANNOTATION: a.SPDX_PREDICATE}}
    manifest = a.attestation_manifest(existing, layer, IMAGE)
    assert manifest["layers"] == [layer, PROVENANCE]
    assert manifest["subject"] == {"mediaType": a.OCI_MANIFEST, "digest": IMAGE["digest"], "size": 2383}
    assert manifest["artifactType"] == a.ATTESTATION_ARTIFACT and manifest["config"] == a.EMPTY_CONFIG
    assert a.attestation_manifest(None, layer, IMAGE)["layers"] == [layer]


def test_updated_index_swaps_only_this_platform() -> None:
    old_amd = attestation_descriptor("sha256:" + "6" * 64, IMAGE)
    old_arm = attestation_descriptor("sha256:" + "7" * 64, OTHER)
    index = {"schemaVersion": 2, "mediaType": a.OCI_INDEX, "manifests": [IMAGE, old_amd, OTHER, old_arm], "annotations": {"x": "y"}}
    new = a.updated_index(index, IMAGE, {"digest": "sha256:" + "8" * 64, "size": 1200})
    assert [m["digest"] for m in new["manifests"]] == [IMAGE["digest"], "sha256:" + "8" * 64, OTHER["digest"], old_arm["digest"]]
    assert new["manifests"][1]["annotations"][a.REFERENCE_DIGEST] == IMAGE["digest"]
    assert new["annotations"] == {"x": "y"}
    added = a.updated_index({"manifests": [IMAGE]}, IMAGE, {"digest": "sha256:" + "8" * 64, "size": 1200})
    assert len(added["manifests"]) == 2 and added["mediaType"] == a.OCI_INDEX


def test_find_image_needs_exactly_one_platform() -> None:
    assert a.find_image({"manifests": [IMAGE, OTHER]}, "arm64") == OTHER
    with pytest.raises(ValueError):
        a.find_image({"manifests": [OTHER]}, "amd64")


class FakeRegistry:
    def __init__(self, documents: dict[str, Any]) -> None:
        self.documents = documents
        self.blobs: dict[str, bytes] = {}
        self.pushed: dict[str, Any] = {}

    def manifest(self, reference: str) -> tuple[dict, str]:
        doc = self.documents[reference]
        return doc, doc["mediaType"]

    def put_blob(self, data: bytes) -> str:
        digest = a.digest_of(data)
        self.blobs[digest] = data
        return digest

    def put_manifest(self, reference: str, doc: dict) -> tuple[str, int]:
        data = a.canonical(doc)
        self.pushed[reference] = doc
        return a.digest_of(data), len(data)


def test_attach_end_to_end() -> None:
    att_digest = "sha256:" + "6" * 64
    existing = {"schemaVersion": 2, "mediaType": a.OCI_MANIFEST, "artifactType": a.ATTESTATION_ARTIFACT, "config": a.EMPTY_CONFIG, "layers": [PROVENANCE]}
    index = {"schemaVersion": 2, "mediaType": a.OCI_INDEX, "manifests": [IMAGE, attestation_descriptor(att_digest, IMAGE)]}
    registry = FakeRegistry({"amd64-abc1234-40199": index, att_digest: existing})
    summary = a.attach(REF, "amd64", {"spdxVersion": "SPDX-2.3", "packages": [{"name": "bash"}]}, registry)  # type: ignore[arg-type]

    new_index = registry.pushed["amd64-abc1234-40199"]
    attestation = next(m for m in new_index["manifests"] if m.get("annotations", {}).get(a.REFERENCE_TYPE))
    manifest = registry.pushed[attestation["digest"]]
    sbom = json.loads(registry.blobs[manifest["layers"][0]["digest"]])
    assert sbom["predicate"]["packages"] == [{"name": "bash"}]
    assert manifest["layers"][1] == PROVENANCE
    assert summary["packages"] == 1 and summary["kept_layers"] == ["https://slsa.dev/provenance/v1"]
    assert a.EMPTY_CONFIG["digest"] in registry.blobs


def test_main_rejects_non_spdx(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "sbom.json"
    path.write_text(json.dumps({"artifacts": []}))
    monkeypatch.setenv("GHCR_TOKEN", "t")
    assert a.main(["--ref", REF, "--arch", "amd64", "--sbom", str(path)]) == 1
