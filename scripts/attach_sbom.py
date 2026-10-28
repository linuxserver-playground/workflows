#!/usr/bin/env python3
"""
Attach a build's SBOM to its staging image the way BuildKit would, so every released index carries it.

    attach_sbom.py --ref <staging reference> --arch amd64 --sbom out/sbom.amd64.spdx.json

BuildKit's own ``--sbom`` scan is off: the ci run of the arch job writes the packages-only SPDX document
(``sbom.<arch>.spdx.json``) from the same syft run as the Release assets. Before publish-manifest creates the
released tags from the staging reference, this script wraps that document in the in-toto statement BuildKit
emits, adds it to the platform's attestation manifest (replacing an SPDX layer already there, keeping the
provenance) and points the staging tag at the new index. ``docker buildx imagetools create`` then copies it into
the released index, where ``docker buildx imagetools inspect --format '{{json .SBOM}}'``, Docker Scout and other
tools that read BuildKit attestations find it.

Registry access goes through the token endpoint with GHCR_USER and GHCR_TOKEN (the workflow's GITHUB_TOKEN with
packages: write). The functions that build the documents are pure and unit-tested.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import netretry

OCI_INDEX = "application/vnd.oci.image.index.v1+json"
OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
DOCKER_LIST = "application/vnd.docker.distribution.manifest.list.v2+json"
DOCKER_MANIFEST = "application/vnd.docker.distribution.manifest.v2+json"
IN_TOTO = "application/vnd.in-toto+json"
STATEMENT = "https://in-toto.io/Statement/v1"
SPDX_PREDICATE = "https://spdx.dev/Document"
PREDICATE_ANNOTATION = "in-toto.io/predicate-type"
ATTESTATION_ARTIFACT = "application/vnd.docker.attestation.manifest.v1+json"
REFERENCE_DIGEST = "vnd.docker.reference.digest"
REFERENCE_TYPE = "vnd.docker.reference.type"
EMPTY_CONFIG = {
    "mediaType": "application/vnd.oci.empty.v1+json",
    "digest": "sha256:44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
    "size": 2,
    "data": "e30=",
}
ACCEPT = ", ".join([OCI_INDEX, DOCKER_LIST, OCI_MANIFEST, DOCKER_MANIFEST])

Json = dict[str, Any]


def parse_ref(ref: str) -> tuple[str, str, str]:
    """'ghcr.io/linuxserver-playground/build/x:tag' -> ('ghcr.io', 'linuxserver-playground/build/x', 'tag')."""
    registry, _, rest = ref.partition("/")
    if not rest or "." not in registry:
        raise ValueError(f"{ref!r} is not a registry reference")
    if "@" in rest:
        raise ValueError(f"{ref!r} is a digest reference; the staging tag is needed to re-point it")
    repository, sep, tag = rest.rpartition(":")
    if not sep or "/" in tag:
        raise ValueError(f"{ref!r} has no tag")
    return registry, repository, tag


def canonical(doc: Json) -> bytes:
    return json.dumps(doc, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def digest_of(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def platform_string(platform: Json) -> str:
    parts = [platform.get("os", "linux"), platform.get("architecture", "")]
    if platform.get("variant"):
        parts.append(platform["variant"])
    return "/".join(parts)


def statement(spdx: Json, ref: str, image: Json) -> Json:
    """The in-toto statement BuildKit's SBOM generator produces, with its subject filled in as BuildKit does."""
    registry, repository, tag = parse_ref(ref)
    platform = urllib.parse.quote(platform_string(image.get("platform") or {}), safe="")
    name = f"pkg:docker/{registry}/{repository}@{tag}?platform={platform}"
    return {
        "_type": STATEMENT,
        "predicateType": SPDX_PREDICATE,
        "subject": [{"name": name, "digest": {"sha256": image["digest"].removeprefix("sha256:")}}],
        "predicate": spdx,
    }


def attestation_manifest(existing: Json | None, sbom_layer: Json, image: Json) -> Json:
    """The platform's attestation manifest with the SBOM as its first layer and every non-SPDX layer kept."""
    kept = [
        layer
        for layer in (existing or {}).get("layers", [])
        if (layer.get("annotations") or {}).get(PREDICATE_ANNOTATION) != SPDX_PREDICATE
    ]
    manifest: Json = {
        "schemaVersion": 2,
        "mediaType": OCI_MANIFEST,
        "artifactType": (existing or {}).get("artifactType", ATTESTATION_ARTIFACT),
        "config": (existing or {}).get("config", EMPTY_CONFIG),
        "layers": [sbom_layer, *kept],
        "subject": {"mediaType": image["mediaType"], "digest": image["digest"], "size": image["size"]},
    }
    return manifest


def find_image(index: Json, arch: str) -> Json:
    images = [
        m
        for m in index.get("manifests", [])
        if (m.get("platform") or {}).get("os") == "linux" and (m.get("platform") or {}).get("architecture") == arch
    ]
    if len(images) != 1:
        raise ValueError(f"expected one linux/{arch} manifest in the index, found {len(images)}")
    return images[0]


def find_attestation(index: Json, image_digest: str) -> Json | None:
    for m in index.get("manifests", []):
        annotations = m.get("annotations") or {}
        if annotations.get(REFERENCE_TYPE) == "attestation-manifest" and annotations.get(REFERENCE_DIGEST) == image_digest:
            return m
    return None


def updated_index(index: Json, image: Json, attestation: Json) -> Json:
    """The index with the image's attestation descriptor replaced (or added right after the image)."""
    descriptor = {
        "mediaType": OCI_MANIFEST,
        "digest": attestation["digest"],
        "size": attestation["size"],
        "annotations": {REFERENCE_DIGEST: image["digest"], REFERENCE_TYPE: "attestation-manifest"},
        "platform": {"architecture": "unknown", "os": "unknown"},
    }
    manifests: list[Json] = []
    for m in index.get("manifests", []):
        annotations = m.get("annotations") or {}
        if annotations.get(REFERENCE_TYPE) == "attestation-manifest" and annotations.get(REFERENCE_DIGEST) == image["digest"]:
            continue
        manifests.append(m)
        if m.get("digest") == image["digest"]:
            manifests.append(descriptor)
    out = {k: v for k, v in index.items() if k != "manifests"}
    out["manifests"] = manifests
    out.setdefault("schemaVersion", 2)
    out["mediaType"] = OCI_INDEX
    return out


class Registry:
    def __init__(self, registry: str, repository: str, user: str, token: str) -> None:
        self.base = f"https://{registry}/v2/{repository}"
        scope = urllib.parse.quote(f"repository:{repository}:pull,push", safe="")
        basic = base64.b64encode(f"{user}:{token}".encode()).decode()
        request = urllib.request.Request(
            f"https://{registry}/token?scope={scope}&service={registry}", headers={"Authorization": f"Basic {basic}"}
        )
        with netretry.retry(lambda: urllib.request.urlopen(request, timeout=60), what="GHCR token") as response:
            self.bearer = json.load(response)["token"]

    def _request(self, method: str, url: str, data: bytes | None = None, headers: dict[str, str] | None = None) -> Any:
        """One registry call, retried on network failures and 5xx/429 (every call here is safe to repeat: blobs and
        manifests are PUT by digest or tag, and a second upload session only abandons the first)."""
        request = urllib.request.Request(url, data=data, method=method, headers={"Authorization": f"Bearer {self.bearer}", **(headers or {})})
        return netretry.retry(lambda: urllib.request.urlopen(request, timeout=300), what=f"{method} {url}")

    def manifest(self, reference: str) -> tuple[Json, str]:
        with self._request("GET", f"{self.base}/manifests/{reference}", headers={"Accept": ACCEPT}) as response:
            body = response.read()
            media_type = response.headers.get("Content-Type", "").split(";")[0]
        doc = json.loads(body)
        return doc, doc.get("mediaType") or media_type

    def has_blob(self, digest: str) -> bool:
        try:
            with self._request("HEAD", f"{self.base}/blobs/{digest}"):
                return True
        except urllib.error.HTTPError as err:
            if err.code == 404:
                return False
            raise

    def put_blob(self, data: bytes) -> str:
        digest = digest_of(data)
        if self.has_blob(digest):
            return digest
        with self._request("POST", f"{self.base}/blobs/uploads/", data=b"") as response:
            location = response.headers["Location"]
        url = urllib.parse.urljoin(self.base + "/", location)
        url += ("&" if "?" in url else "?") + "digest=" + urllib.parse.quote(digest)
        with self._request("PUT", url, data=data, headers={"Content-Type": "application/octet-stream"}):
            pass
        return digest

    def put_manifest(self, reference: str, doc: Json) -> tuple[str, int]:
        data = canonical(doc)
        with self._request("PUT", f"{self.base}/manifests/{reference}", data=data, headers={"Content-Type": doc["mediaType"]}):
            pass
        return digest_of(data), len(data)


def attach(ref: str, arch: str, spdx: Json, registry: Registry) -> Json:
    """Attach `spdx` to the linux/<arch> image behind `ref`; returns a summary for the log."""
    _, _, tag = parse_ref(ref)
    doc, media_type = registry.manifest(tag)
    if media_type in (OCI_INDEX, DOCKER_LIST) or "manifests" in doc:
        index = doc
        image = find_image(index, arch)
    else:
        # A single image manifest (no provenance): wrap it in an index of its own.
        data = canonical(doc)
        image = {"mediaType": media_type, "digest": digest_of(data), "size": len(data), "platform": {"architecture": arch, "os": "linux"}}
        index = {"schemaVersion": 2, "mediaType": OCI_INDEX, "manifests": [image]}
    existing_descriptor = find_attestation(index, image["digest"])
    existing = registry.manifest(existing_descriptor["digest"])[0] if existing_descriptor else None

    body = canonical(statement(spdx, ref, image))
    layer = {"mediaType": IN_TOTO, "digest": registry.put_blob(body), "size": len(body), "annotations": {PREDICATE_ANNOTATION: SPDX_PREDICATE}}
    manifest = attestation_manifest(existing, layer, image)
    if manifest["config"].get("digest") == EMPTY_CONFIG["digest"]:
        registry.put_blob(base64.b64decode(EMPTY_CONFIG["data"]))
    digest, size = registry.put_manifest(digest_of(canonical(manifest)), manifest)
    new_index = updated_index(index, image, {"digest": digest, "size": size})
    index_digest, _ = registry.put_manifest(tag, new_index)
    return {
        "ref": ref,
        "arch": arch,
        "image": image["digest"],
        "sbom_bytes": len(body),
        "packages": len(spdx.get("packages", [])),
        "kept_layers": [(lay.get("annotations") or {}).get(PREDICATE_ANNOTATION) for lay in manifest["layers"][1:]],
        "attestation": digest,
        "index": index_digest,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--ref", required=True, help="staging reference the released tags are created from")
    parser.add_argument("--arch", required=True)
    parser.add_argument("--sbom", required=True, type=Path, help="SPDX JSON written by the ci harness")
    args = parser.parse_args(argv)
    spdx = json.loads(args.sbom.read_text(encoding="utf-8"))
    if not str(spdx.get("spdxVersion", "")).startswith("SPDX-"):
        print(f"::error::{args.sbom} is not an SPDX document", file=sys.stderr)
        return 1
    user, token = os.environ.get("GHCR_USER", "x"), os.environ.get("GHCR_TOKEN", "")
    if not token:
        print("::error::GHCR_TOKEN is not set", file=sys.stderr)
        return 1
    registry_host, repository, _ = parse_ref(args.ref)
    try:
        summary = attach(args.ref, args.arch, spdx, Registry(registry_host, repository, user, token))
    except (urllib.error.URLError, ValueError, KeyError) as err:
        detail = err.read().decode("utf-8", "replace")[:500] if isinstance(err, urllib.error.HTTPError) else ""
        print(f"::error::could not attach the SBOM to {args.ref}: {err} {detail}".rstrip(), file=sys.stderr)
        return 1
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
