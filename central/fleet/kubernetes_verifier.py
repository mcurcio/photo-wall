"""Read-only D17 measurements and signed deployment-owner evidence verification.

Kubernetes observation cannot prevent endpoint mutation. A trusted deployment
owner must issue the bounded, irrevocable routing hold described in the runbook;
without that signed live record this adapter cannot certify or measure authority.
All HTTP operations are GET. No deployment, Lease, Secret or gate is mutated here.
"""
from __future__ import annotations

import json
import re
import ssl
import time
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from urllib.parse import quote

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from central.fleet.rollout_gate import (
    MeasuredServingImage,
    RolloutCertification,
    RolloutGateError,
    _scope,
)
from contracts.strict_json import loads_object

MAX_DOCUMENT = 4 * 1024 * 1024
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_STANDARD_GROUPS = frozenset({
    "apps", "batch", "autoscaling", "policy", "networking.k8s.io", "discovery.k8s.io",
    "rbac.authorization.k8s.io", "authorization.k8s.io", "authentication.k8s.io",
    "admissionregistration.k8s.io", "apiextensions.k8s.io", "apiregistration.k8s.io",
    "certificates.k8s.io", "coordination.k8s.io", "events.k8s.io", "flowcontrol.apiserver.k8s.io",
    "node.k8s.io", "scheduling.k8s.io", "storage.k8s.io", "resource.k8s.io",
    "internal.apiserver.k8s.io", "gateway.networking.k8s.io",
})


def _canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _hash(value) -> str:
    return sha256(_canonical(value)).hexdigest()


def _fail(reason):
    raise RolloutGateError("kubernetes_" + reason)


def _name(value):
    if type(value) is not str or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,252}", value):
        _fail("identity_invalid")
    return quote(value, safe="")


class KubernetesReader:
    """Bounded authenticated API reader, including complete list pagination."""

    def __init__(self, client: httpx.Client, token_path: Path | None = None):
        self.client, self.token_path = client, token_path

    @classmethod
    def in_cluster(cls):
        root = Path("/var/run/secrets/kubernetes.io/serviceaccount")
        return cls(httpx.Client(base_url="https://kubernetes.default.svc",
                               verify=ssl.create_default_context(cafile=str(root / "ca.crt")),
                               timeout=5, follow_redirects=False), root / "token")

    def get(self, path: str, params=None) -> dict:
        if not path.startswith(("/api/", "/apis")) or "?" in path:
            _fail("api_path_invalid")
        try:
            headers = {}
            if self.token_path:
                token = self.token_path.read_text().strip()
                if not token or len(token) > 16384:
                    _fail("credential_unavailable")
                headers["Authorization"] = "Bearer " + token
            with self.client.stream("GET", path, params=params, headers=headers) as response:
                response.raise_for_status()
                raw = bytearray()
                for chunk in response.iter_bytes():
                    raw.extend(chunk)
                    if len(raw) > MAX_DOCUMENT:
                        _fail("inventory_too_large")
            value = loads_object(bytes(raw), max_bytes=MAX_DOCUMENT)
            if value is None:
                _fail("response_invalid")
            return value
        except RolloutGateError:
            raise
        except (httpx.HTTPError, OSError, ValueError) as exc:
            raise RolloutGateError("kubernetes_inventory_unavailable") from exc

    def listing(self, path: str) -> list[dict]:
        items, cursor, seen = [], "", set()
        for _ in range(64):
            page = self.get(path, {"limit": 500, "continue": cursor})
            if type(page.get("items")) is not list or any(type(x) is not dict for x in page["items"]):
                _fail("list_invalid")
            items.extend(page["items"])
            cursor = page.get("metadata", {}).get("continue", "")
            if not cursor:
                return sorted(items, key=lambda x: (x.get("metadata", {}).get("namespace", ""),
                                                   x.get("metadata", {}).get("name", "")))
            if type(cursor) is not str or cursor in seen or len(items) > 20000:
                _fail("list_incomplete")
            seen.add(cursor)
        _fail("list_incomplete")


@dataclass(frozen=True)
class KubernetesIdentity:
    namespace: str
    pod_name: str
    pod_uid: str
    container_name: str
    deployment_name: str
    deployment_uid: str

    @classmethod
    def from_directory(cls, directory: Path):
        # Files are deployment-owned downward-API/config projection, never a
        # caller's image digest assertion. Kubernetes resolves actual imageID.
        names = ("namespace", "pod_name", "pod_uid", "container_name", "deployment_name", "deployment_uid")
        return cls(*(directory.joinpath(name).read_text().strip() for name in names))


@dataclass(frozen=True)
class TopologyObservation:
    deployment_uid: str
    generation: int
    serving_images: tuple[str, ...]
    local_image: str
    endpoints_sha256: str
    routes_sha256: str
    local_measurement_ref: str


def _owner(obj: dict, kind: str) -> str:
    owners = [x for x in obj.get("metadata", {}).get("ownerReferences", [])
              if x.get("controller") is True and x.get("kind") == kind]
    if len(owners) != 1 or not owners[0].get("uid"):
        _fail("controller_identity_unknown")
    return owners[0]["uid"]


def _image(pod: dict, container: str) -> str:
    if pod.get("metadata", {}).get("deletionTimestamp") or pod.get("spec", {}).get("hostNetwork"):
        _fail("pod_not_admissible")
    status = [x for x in pod.get("status", {}).get("containerStatuses", []) if x.get("name") == container]
    if len(status) != 1 or status[0].get("ready") is not True or "running" not in status[0].get("state", {}):
        _fail("running_image_unknown")
    image_id = status[0].get("imageID", "")
    # Config hashes (containerd://sha256:...) are not registry content digests.
    # Require a CRI registry-qualified repository@sha256 content identity.
    if "@" not in image_id:
        _fail("image_content_identity_unknown")
    digest = image_id.rsplit("@", 1)[1]
    if _DIGEST.fullmatch(digest) is None:
        _fail("image_content_identity_unknown")
    return digest


class KubernetesTopology:
    def __init__(self, reader: KubernetesReader, identity: KubernetesIdentity):
        self.reader, self.identity = reader, identity

    def observe(self) -> TopologyObservation:
        api, identity = self.reader, self.identity
        ns, deployment_name = _name(identity.namespace), _name(identity.deployment_name)
        deployment = api.get(f"/apis/apps/v1/namespaces/{ns}/deployments/{deployment_name}")
        if deployment.get("metadata", {}).get("uid") != identity.deployment_uid:
            _fail("deployment_replaced")
        generation = deployment.get("metadata", {}).get("generation")
        if type(generation) is not int or generation < 1:
            _fail("deployment_generation_unknown")
        sets = api.listing(f"/apis/apps/v1/namespaces/{ns}/replicasets")
        owned = {x["metadata"]["uid"] for x in sets
                 if any(y.get("controller") is True and y.get("kind") == "Deployment"
                        and y.get("uid") == identity.deployment_uid
                        for y in x.get("metadata", {}).get("ownerReferences", []))}
        pods = api.listing(f"/api/v1/namespaces/{ns}/pods")
        pods_by_uid = {x.get("metadata", {}).get("uid"): x for x in pods}
        local = pods_by_uid.get(identity.pod_uid)
        if (local is None or local.get("metadata", {}).get("name") != identity.pod_name
                or _owner(local, "ReplicaSet") not in owned):
            _fail("local_pod_identity_changed")
        local_image = _image(local, identity.container_name)
        services = api.listing("/api/v1/services")
        slices = api.listing("/apis/discovery.k8s.io/v1/endpointslices")
        owned_uids = {p["metadata"]["uid"] for p in pods
                      if any(o.get("uid") in owned for o in p.get("metadata", {}).get("ownerReferences", []))}
        manual_names = {s.get("metadata", {}).get("labels", {}).get("kubernetes.io/service-name") for s in slices
                        if s.get("metadata", {}).get("namespace") == identity.namespace
                        and any(e.get("targetRef", {}).get("uid") in owned_uids for e in s.get("endpoints", []))}
        selected = []
        for service in services:
            if service.get("metadata", {}).get("namespace") != identity.namespace:
                continue
            selector = service.get("spec", {}).get("selector", {})
            if service["metadata"]["name"] in manual_names or selector and any(all(p.get("metadata", {}).get("labels", {}).get(k) == v for k, v in selector.items())
                                for p in pods if any(o.get("uid") in owned for o in p.get("metadata", {}).get("ownerReferences", []))):
                selected.append(service)
        if not selected:
            _fail("serving_service_unknown")
        names = {s["metadata"]["name"] for s in selected}
        serving = {_image(p, identity.container_name) for p in pods
                   if any(o.get("uid") in owned for o in p.get("metadata", {}).get("ownerReferences", []))}
        scoped_slices = []
        for item in slices:
            metadata = item.get("metadata", {})
            if metadata.get("namespace") != identity.namespace or metadata.get("labels", {}).get("kubernetes.io/service-name") not in names:
                continue
            scoped_slices.append(item)
            for endpoint in item.get("endpoints", []):
                # Unknown readiness is potentially serving, never safe exclusion.
                if endpoint.get("conditions", {}).get("ready") is False and endpoint.get("conditions", {}).get("serving") is False:
                    continue
                ref = endpoint.get("targetRef", {})
                pod = pods_by_uid.get(ref.get("uid"))
                if (ref.get("kind") != "Pod" or pod is None or _owner(pod, "ReplicaSet") not in owned
                        or not set(endpoint.get("addresses", [])) <= {x.get("ip") for x in pod.get("status", {}).get("podIPs", [])}):
                    _fail("endpoint_identity_unknown")
                serving.add(_image(pod, identity.container_name))
        if not serving or local_image not in serving:
            _fail("serving_images_unknown")
        groups = api.get("/apis").get("groups")
        if type(groups) is not list or any(g.get("name") not in _STANDARD_GROUPS for g in groups):
            _fail("routing_api_unsupported")
        routes = {"services": services, "ingresses": api.listing("/apis/networking.k8s.io/v1/ingresses"),
                  "networkpolicies": api.listing("/apis/networking.k8s.io/v1/networkpolicies")}
        for group in groups:
            if group["name"] != "gateway.networking.k8s.io":
                continue
            version = group.get("preferredVersion", {}).get("groupVersion")
            if version not in {"gateway.networking.k8s.io/v1", "gateway.networking.k8s.io/v1beta1"}:
                _fail("routing_api_unsupported")
            resources = api.get("/apis/" + version).get("resources", [])
            for resource in resources:
                name = resource.get("name", "")
                if "/" in name:
                    continue
                if name not in {"gatewayclasses", "gateways", "httproutes", "grpcroutes", "referencegrants"}:
                    _fail("routing_api_unsupported")
                routes[name] = api.listing(f"/apis/{version}/{name}")
        # Include Deployment + owned Pod image/status to bind readiness and
        # direct Pod reachability, not only the selected Gateway.
        routes["deployment"] = deployment
        routes["pods"] = [p for p in pods if any(o.get("uid") in owned for o in p.get("metadata", {}).get("ownerReferences", []))]
        return TopologyObservation(identity.deployment_uid, generation, tuple(sorted(serving)), local_image,
            _hash(scoped_slices), _hash(routes), f"kubernetes:pod/{identity.pod_uid}/{identity.container_name}")


class PostgresEvidenceWatermarks:
    def __init__(self, db):
        self.db = db

    def observe(self, *, audience, role, authority, name, uid, generation, payload_sha256):
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO node_rollout_evidence_heads VALUES(%s,%s,%s,%s,%s,%s,%s) "
                         "ON CONFLICT DO NOTHING", (audience, role, authority, name, uid, generation, payload_sha256))
            row = conn.execute("SELECT * FROM node_rollout_evidence_heads WHERE audience=%s "
                               "AND authority_role=%s AND record_name=%s FOR UPDATE", (audience, role, name)).fetchone()
            if row["authority_sha256"] != authority or row["record_uid"] != uid:
                _fail("evidence_authority_reprovisioning_required")
            if (generation < row["generation"] or generation == row["generation"]
                    and row["payload_sha256"] != payload_sha256):
                _fail("evidence_replayed")
            if generation > row["generation"]:
                conn.execute("UPDATE node_rollout_evidence_heads SET generation=%s,payload_sha256=%s "
                             "WHERE audience=%s AND authority_role=%s AND record_name=%s",
                             (generation, payload_sha256, audience, role, name))


class SignedRolloutEvidence:
    """Live ConfigMap records signed by separate CI and deployment-guard keys.

    The guard signature commits to an irrevocable no-routing-mutation interval,
    even after early revocation or reader failure; revocation denies subsequent
    admissions but does not shorten the promised interval. External tooling must
    keep unsafe rollback out after expiry while unresolved effects remain.
    """
    def __init__(self, reader, *, namespace, ci_record, guard_record, ci_public_key: bytes,
                 guard_public_key: bytes, audience: str, watermarks: PostgresEvidenceWatermarks, clock=time.time):
        self.reader, self.namespace = reader, _name(namespace)
        self.ci_record, self.guard_record = _name(ci_record), _name(guard_record)
        if ci_public_key == guard_public_key:
            _fail("distinct_authorities_required")
        self.watermarks = watermarks
        self.key_hashes = {"ci": sha256(ci_public_key).hexdigest(), "guard": sha256(guard_public_key).hexdigest()}
        self.ci_key = Ed25519PublicKey.from_public_bytes(ci_public_key)
        self.guard_key = Ed25519PublicKey.from_public_bytes(guard_public_key)
        self.audience, self.clock = audience, clock

    def _read(self, name, key, domain, role):
        record = self.reader.get(f"/api/v1/namespaces/{self.namespace}/configmaps/{name}")
        raw = record.get("data", {}).get("evidence.json", "").encode()
        value = loads_object(raw, max_bytes=65536)
        try:
            if value is None or set(value) != {"payload", "signature"} or type(value["payload"]) is not dict:
                _fail("evidence_invalid")
            payload = value["payload"]
            common = {"audience", "record_uid", "generation", "state", "issued_at", "expires_at"}
            fields = ({"image_digests", "compatibility_matrix_sha256", "fence_contract_sha256", "readiness_contract_sha256"}
                      if role == "ci" else {"deployment_uid", "deployment_generation", "serving_image_digests",
                      "rollback_image_digests", "endpoint_slice_sha256", "route_inventory_sha256", "ci_payload_sha256",
                      "mutation_not_before", "post_expiry_policy"})
            if set(payload) != common | fields:
                _fail("evidence_shape_invalid")
            key.verify(bytes.fromhex(value["signature"]), domain + _canonical(payload))
            uid = record.get("metadata", {}).get("uid")
            if (payload.get("audience") != self.audience or type(uid) is not str or not uid
                    or payload.get("record_uid") != uid or type(payload.get("generation")) is not int
                    or payload["generation"] < 1):
                _fail("evidence_identity_invalid")
            # Commit the signed revocation's generation before rejecting state;
            # an older active record must stay rejected across process restart.
            self.watermarks.observe(audience=self.audience, role=role, authority=self.key_hashes[role],
                name=self.namespace+"/"+name, uid=uid, generation=payload["generation"], payload_sha256=_hash(payload))
            now = self.clock()
            if (payload.get("audience") != self.audience or payload.get("state") != "active"
                    or type(payload.get("issued_at")) not in (int, float)
                    or type(payload.get("expires_at")) not in (int, float)
                    or not payload["issued_at"] <= now < payload["expires_at"]
                    or payload["expires_at"]-payload["issued_at"] > 300):
                _fail("evidence_stale_or_revoked")
            return payload
        except RolloutGateError:
            raise
        except (InvalidSignature, ValueError, TypeError, KeyError) as exc:
            raise RolloutGateError("kubernetes_evidence_invalid") from exc

    def verify(self, observed: TopologyObservation) -> RolloutCertification:
        ci = self._read(self.ci_record, self.ci_key, b"photo-wall-rollout-ci-v1\0", "ci")
        guard = self._read(self.guard_record, self.guard_key, b"photo-wall-rollout-guard-v1\0", "guard")
        scope = {"deployment_uid": observed.deployment_uid, "deployment_generation": observed.generation,
                 "serving_image_digests": list(observed.serving_images),
                 "endpoint_slice_sha256": observed.endpoints_sha256, "route_inventory_sha256": observed.routes_sha256}
        if any(guard.get(k) != v for k, v in scope.items()):
            _fail("evidence_topology_changed")
        rollback = guard.get("rollback_image_digests")
        if (type(rollback) is not list or not rollback or rollback != sorted(set(rollback))
                or any(type(x) is not str or not _DIGEST.fullmatch(x) for x in rollback)):
            _fail("rollback_evidence_missing")
        if (ci.get("image_digests") != sorted(set(observed.serving_images) | set(rollback))
                or guard.get("ci_payload_sha256") != _hash(ci)
                or guard.get("mutation_not_before") != guard["expires_at"]
                or guard.get("post_expiry_policy") != "retain_fenced_images_until_all_effects_reconciled"
                or type(guard.get("generation")) is not int or guard["generation"] < 1):
            _fail("guard_contract_invalid")
        for key in ("compatibility_matrix_sha256", "fence_contract_sha256", "readiness_contract_sha256"):
            if type(ci.get(key)) is not str or not _HEX.fullmatch(ci[key]):
                _fail("image_qualification_missing")
        return RolloutCertification(observed.deployment_uid, observed.generation, observed.serving_images,
            tuple(rollback), observed.endpoints_sha256, observed.routes_sha256,
            ci["compatibility_matrix_sha256"], ci["fence_contract_sha256"], ci["readiness_contract_sha256"],
            f"kubernetes:guard/{self.namespace}/{self.guard_record}/{guard['generation']}",
            self.clock(), min(ci["expires_at"], guard["expires_at"]))


class KubernetesRolloutVerifier:
    def __init__(self, topology: KubernetesTopology, evidence: SignedRolloutEvidence):
        self.topology, self.evidence = topology, evidence

    @contextmanager
    def certify(self):
        # The signed guard's hold persists until expiry independently of this
        # HTTP reader. A second read catches an inventory/evidence read race.
        observed = self.topology.observe()
        certificate = self.evidence.verify(observed)
        if self.topology.observe() != observed:
            _fail("topology_changed_during_observation")
        yield certificate

    def measure(self) -> MeasuredServingImage:
        observed = self.topology.observe()
        certificate = self.evidence.verify(observed)
        return MeasuredServingImage(observed.deployment_uid, observed.local_image,
                                    observed.local_measurement_ref, _hash(_scope(certificate)), certificate.expires_at)


def configured_verifier(db, path: Path) -> KubernetesRolloutVerifier:
    """Deployment-owned public configuration; supplying it never opens the gate."""
    raw = path.read_bytes()
    config = loads_object(raw, max_bytes=8192)
    expected = {"identity_directory", "namespace", "ci_record", "guard_record", "ci_public_key",
                "guard_public_key", "audience"}
    if config is None or set(config) != expected or any(type(v) is not str or not v for v in config.values()):
        _fail("configuration_invalid")
    api = KubernetesReader.in_cluster()
    identity = KubernetesIdentity.from_directory(Path(config["identity_directory"]))
    evidence = SignedRolloutEvidence(api, namespace=config["namespace"], ci_record=config["ci_record"],
        guard_record=config["guard_record"], ci_public_key=bytes.fromhex(config["ci_public_key"]),
        guard_public_key=bytes.fromhex(config["guard_public_key"]), audience=config["audience"],
        watermarks=PostgresEvidenceWatermarks(db))
    return KubernetesRolloutVerifier(KubernetesTopology(api, identity), evidence)
