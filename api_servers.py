"""Resolve the effective OpenAPI server URL for an operation."""

from __future__ import annotations

import re
from urllib.parse import urlsplit


def operation_url(openapi: dict, path: str, method: str) -> str:
    """Use operation, path, then root servers in OpenAPI precedence order."""
    path_item = (openapi.get("paths") or {}).get(path)
    operation = path_item.get(method.lower()) if isinstance(path_item, dict) else None
    if not isinstance(operation, dict):
        raise ValueError(f"OpenAPI operation {method.upper()} {path} is unavailable")
    servers = next((scope["servers"] for scope in (operation, path_item, openapi)
                    if "servers" in scope), None)
    if not isinstance(servers, list) or not servers or not isinstance(servers[0], dict):
        raise ValueError(f"OpenAPI operation {method.upper()} {path} needs a server URL")
    server = servers[0]
    base = server.get("url")
    if not isinstance(base, str) or not base:
        raise ValueError(f"OpenAPI operation {method.upper()} {path} needs a server URL")

    variables = server.get("variables") or {}
    if not isinstance(variables, dict):
        raise ValueError(f"OpenAPI operation {method.upper()} {path} has invalid server variables")

    def replace(match: re.Match) -> str:
        name = match.group(1)
        spec = variables.get(name)
        if not isinstance(spec, dict):
            raise ValueError(f"OpenAPI server variable {name!r} has no definition")
        value = spec.get("default")
        allowed = spec.get("enum")
        if (not isinstance(value, str) or not value or
                allowed is not None and (not isinstance(allowed, list) or value not in allowed)):
            raise ValueError(f"OpenAPI server variable {name!r} needs a valid default")
        return value

    base = re.sub(r"\{([^{}]+)\}", replace, base).rstrip("/")
    parsed = urlsplit(base)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.query or parsed.fragment:
        raise ValueError(f"OpenAPI operation {method.upper()} {path} needs an absolute HTTP server URL")
    if re.search(r"\{[^{}]+\}", base):
        raise ValueError(f"OpenAPI operation {method.upper()} {path} has unresolved server variable")
    return base if base.endswith(path) else base + "/" + path.lstrip("/")
