from __future__ import annotations

import json
from urllib.parse import urljoin, urlparse

from crawler.models import CrawlResult

AI_DISCOVERY_PATHS = [
    "/.well-known/ai-plugin.json",
    "/.well-known/openid-configuration",
    "/.well-known/security.txt",
    "/openapi.json",
    "/swagger.json",
    "/v1/models",
    "/health",
    "/metrics",
]


def seed_agent_endpoints(start_url: str) -> list[str]:
    return [urljoin(start_url, path) for path in AI_DISCOVERY_PATHS]


def classify_endpoint(url: str, content_type: str | None) -> str:
    path = urlparse(url).path.lower()
    if path.endswith(("openapi.json", "swagger.json")):
        return "api_schema"
    if path.endswith("/v1/models"):
        return "model_catalog"
    if path.endswith("/metrics"):
        return "metrics"
    if path.endswith("/health"):
        return "health"
    if path.startswith("/.well-known/"):
        return "well_known"
    if content_type and "json" in content_type.lower():
        return "json_api"
    if content_type and "html" in content_type.lower():
        return "web_ui"
    return "other"


def build_agent_inventory(results: list[CrawlResult]) -> dict[str, object]:
    endpoints: list[dict[str, object]] = []
    models: set[str] = set()

    for result in results:
        endpoint = {
            "url": result.url,
            "status": result.status,
            "type": classify_endpoint(result.url, result.content_type),
            "content_type": result.content_type,
            "error": result.error,
        }
        endpoints.append(endpoint)

        if result.metadata:
            for model in result.metadata.get("models", []):
                if isinstance(model, str):
                    models.add(model)

    return {
        "endpoints": endpoints,
        "model_names": sorted(models),
        "total_endpoints": len(endpoints),
    }


def build_service_graph(results: list[CrawlResult]) -> dict[str, object]:
    nodes: dict[str, dict[str, object]] = {}
    edges: list[dict[str, str]] = []

    for result in results:
        parsed = urlparse(result.url)
        host = parsed.netloc
        nodes.setdefault(host, {"id": host, "kind": "host"})

        endpoint_id = result.url
        nodes[endpoint_id] = {
            "id": endpoint_id,
            "kind": classify_endpoint(result.url, result.content_type),
            "status": result.status,
        }
        edges.append({"from": host, "to": endpoint_id, "relation": "exposes"})

        for link in result.links:
            edges.append({"from": endpoint_id, "to": link, "relation": "links_to"})

    return {"nodes": list(nodes.values()), "edges": edges}


def write_json(path: str, payload: dict[str, object]) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
