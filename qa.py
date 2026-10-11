"""Offline checks for a generated Freya workflow and a wrapper for practice runs.

The validator catches errors that the workflow API may silently strip. It is a
preflight check, not a substitute for the API dry run or a scored simulation.
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
import tempfile
from collections import deque
from itertools import product
from pathlib import Path

from api_bindings import (account_response_collection, identity_response_fields,
                          identity_selector_field, note_request_fields,
                          promise_request_fields, transfer_request_fields,
                          close_request_fields)
from api_parameters import required_header_bindings
from api_body_bindings import fixed_required_body_values
from api_servers import operation_url
from customer_bindings import customer_identifier_field
from openapi_resolver import normalize_openapi
from write_result_fields import write_success_field


ROOT = Path(__file__).resolve().parent
_BANK_TOOLS = frozenset({
    "verify_identity", "get_account", "create_promise", "add_note",
    "schedule_callback", "transfer_to_agent", "close_call",
})
_ACCOUNT_TOOLS = frozenset({"get_account", "create_promise", "add_note"})
_NODE_TYPES = frozenset({"conversation", "api_request", "code", "end_call"})
_SECRET = re.compile(
    r"(?:\bfak_[A-Za-z0-9_-]{8,}\b|\bsk-[A-Za-z0-9_-]{8,}\b|"
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|"
    r"\bBearer\s+(?!\{\{|\$\{)[A-Za-z0-9._~+/-]{12,})",
    re.I,
)


def _walk(value, path="workflow"):
    yield path, value
    if isinstance(value, dict):
        for key, child in value.items():
            yield from _walk(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk(child, f"{path}[{index}]")


def _condition_has_verification(condition, complete_variable: str | None = None) -> bool:
    """Recognize a positive, deterministic identity gate on an edge."""
    if not isinstance(condition, dict):
        return False
    children = condition.get("conditions")
    if isinstance(children, list):
        logic = condition.get("logic")
        if logic == "and":
            return any(_condition_has_verification(c, complete_variable) for c in children)
        if logic == "or":
            return bool(children) and all(_condition_has_verification(c, complete_variable) for c in children)
        return False
    variable = str(condition.get("variable", "")).lower()
    operand = str(condition.get("operand", "")).lower()
    if complete_variable is not None:
        return (condition.get("operator") == "equals" and variable == complete_variable.lower()
                and operand in {"true", "verified", "passed", "success"})
    identity_variable = (
        "all_verified" in variable or
        ("identity" in variable or "verification" in variable) and
        ("status" in variable or "state" in variable or "verified" in variable)
    )
    return (
        condition.get("operator") == "equals"
        and identity_variable
        and operand in {"true", "verified", "passed", "success"}
    )


def _condition_has_confirmation(condition) -> bool:
    """Prove an edge requires a positive confirmation state, including groups."""
    if not isinstance(condition, dict):
        return False
    children = condition.get("conditions")
    if isinstance(children, list):
        if condition.get("logic") == "and":
            return any(_condition_has_confirmation(child) for child in children)
        if condition.get("logic") == "or":
            return bool(children) and all(_condition_has_confirmation(child) for child in children)
        return False
    variable = str(condition.get("variable", "")).lower()
    operand = str(condition.get("operand", "")).lower()
    return (condition.get("operator") == "equals" and "confirm" in variable and
            operand in {"true", "confirmed", "yes", "accepted"})


def _condition_variables(condition) -> set[str]:
    if not isinstance(condition, dict):
        return set()
    children = condition.get("conditions")
    if isinstance(children, list):
        return set().union(*(_condition_variables(child) for child in children))
    variable = condition.get("variable")
    return {variable} if isinstance(variable, str) else set()


def _enum_code_write_errors(nodes: dict) -> list[str]:
    """Catch literal code writes that violate an extracted variable's enum."""
    enums: dict[str, set[str]] = {}
    for node in nodes.values():
        if not isinstance(node, dict):
            continue
        schema = node.get("extraction_schema", [])
        for field in schema if isinstance(schema, list) else []:
            if isinstance(field, dict) and isinstance(field.get("name"), str) and field.get("enum_values"):
                enums.setdefault(field["name"], set()).update(str(value) for value in field["enum_values"])
    errors = []
    for name, node in nodes.items():
        if not isinstance(node, dict):
            continue
        actions = node.get("pre_actions", [])
        for action in actions if isinstance(actions, list) else []:
            fn = action.get("function") if isinstance(action, dict) else None
            if not isinstance(fn, dict) or fn.get("handler") != "__function__:code_exec":
                continue
            params = fn.get("parameters")
            props = params.get("properties") if isinstance(params, dict) else None
            code_field = props.get("code") if isinstance(props, dict) else None
            code = code_field.get("default") if isinstance(code_field, dict) else None
            if not isinstance(code, str):
                continue
            try:
                tree = ast.parse(code)
            except SyntaxError:
                continue  # Existing code syntax validation reports this separately.
            for statement in ast.walk(tree):
                if not isinstance(statement, ast.Assign) or not isinstance(statement.value, ast.Constant):
                    continue
                value = statement.value.value
                if not isinstance(value, str):
                    continue
                for target in statement.targets:
                    if (isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name) and
                            target.value.id == "vars" and isinstance(target.slice, ast.Constant)):
                        key = target.slice.value
                        if key in enums and value not in enums[key]:
                            errors.append(f"workflow.nodes.{name}: code writes {key}={value!r} outside extraction enum {sorted(enums[key])}")
    return errors


def _condition_requires(condition, variable: str, operator: str, operand: str) -> bool:
    """Prove a route requires a particular atomic condition."""
    if not isinstance(condition, dict):
        return False
    children = condition.get("conditions")
    if isinstance(children, list):
        checks = [_condition_requires(child, variable, operator, operand) for child in children]
        if condition.get("logic") == "and":
            return any(checks)
        if condition.get("logic") == "or":
            return bool(checks) and all(checks)
        return False
    return (condition.get("variable") == variable and
            condition.get("operator") == operator and
            str(condition.get("operand")) == operand)


def _condition_clauses(condition) -> list[frozenset[tuple[str, str, str]]]:
    """Expand a small transition condition into ORs of required atoms."""
    if not isinstance(condition, dict):
        return []
    children = condition.get("conditions")
    if isinstance(children, list) and children:
        groups = [_condition_clauses(child) for child in children]
        if condition.get("logic") == "or":
            clauses = [clause for group in groups for clause in group]
            return clauses if len(clauses) <= 128 else []
        if condition.get("logic") == "and" and all(groups):
            combinations = product(*groups)
            clauses = [frozenset().union(*parts) for _, parts in zip(range(129), combinations)]
            return clauses if len(clauses) <= 128 else []
        return []
    variable = condition.get("variable")
    operator = condition.get("operator")
    if not isinstance(variable, str) or not isinstance(operator, str):
        return []
    return [frozenset({(variable, operator, str(condition.get("operand", "")).lower())})]


def _promise_success_notice(node) -> bool:
    """Recognize fixed speech that claims a promise was recorded or confirmed."""
    if not isinstance(node, dict):
        return False
    on_enter = node.get("on_enter")
    if not isinstance(on_enter, dict) or on_enter.get("type") != "tts":
        return False
    text = on_enter.get("text", "")
    if isinstance(text, str) and re.search(
            r"\b(?:cannot|can't|could not|not|unable|unknown|whether)\b", text, re.I):
        return False
    return (isinstance(text, str) and
            bool(re.search(r"\bpromis(?:e|es)\b", text, re.I)) and
            bool(re.search(r"\brecord(?:ed|ing)?\b|\bconfirm(?:ed|ing)?\b|\bregister(?:ed)?\b|\bsms\b", text, re.I)))


def _source_guarantees_no_write(description: str) -> bool:
    return bool(re.search(
        r"\b(?:nothing|no (?:promise|write|record|action))\b.{0,50}"
        r"\b(?:recorded|written|persisted|created|carried out|made)\b|"
        r"\b(?:not|never)\b.{0,35}\b(?:recorded|written|persisted|created|carried out)\b|"
        r"\bno write\b",
        description, re.I,
    ))


def _schema_for(openapi: dict, path: str, method: str) -> dict:
    op = openapi.get("paths", {}).get(path, {}).get(method.lower(), {})
    return op.get("requestBody", {}).get("content", {}).get("application/json", {}).get("schema", {})


def _tool_name(tool) -> str | None:
    fn = tool.get("function") if isinstance(tool, dict) else None
    return fn.get("name") if isinstance(fn, dict) else None


def validate_workflow(workflow: dict, openapi: dict) -> list[str]:
    """Return actionable errors for a bank workflow; never makes network calls.

    The bank host, paths, methods and request fields come from ``openapi``, so
    this also works when the final's tool API differs from the practice API.
    """
    errors: list[str] = []
    if not isinstance(workflow, dict):
        return ["workflow must be a JSON object"]
    if not isinstance(openapi, dict) or not isinstance(openapi.get("paths"), dict):
        return ["openapi must contain a paths object"]
    try:
        openapi = normalize_openapi(openapi)
    except ValueError as exc:
        return [f"openapi: {exc}"]

    for path, value in _walk(workflow):
        if value is None:
            errors.append(f"{path}: remove null; omit the optional field instead")
        elif isinstance(value, str) and _SECRET.search(value):
            errors.append(f"{path}: remove the embedded secret; use call variables or environment configuration")
        elif (isinstance(value, str) and value and
              re.search(r"\.headers\.properties\.(?:authorization|x-api-key|x-team-key|api-key)\.default$", path, re.I) and
              "{{" not in value and "${" not in value):
            errors.append(f"{path}: remove the literal credential; use a variable placeholder")

    nodes = workflow.get("nodes")
    globals_ = workflow.get("global_nodes", {})
    if not isinstance(nodes, dict) or not nodes:
        errors.append("workflow.nodes must be a nonempty object")
        nodes = {}
    if not isinstance(globals_, dict):
        errors.append("workflow.global_nodes must be an object")
        globals_ = {}
    initial = workflow.get("initial_node")
    if not isinstance(initial, str) or initial not in nodes:
        errors.append("workflow.initial_node must name a node in workflow.nodes")
    for name in nodes.keys() & globals_.keys():
        errors.append(f"node {name!r} appears in both nodes and global_nodes")
    all_nodes = {**nodes, **globals_}
    errors.extend(_enum_code_write_errors(all_nodes))

    for key in ("system_prompt",):
        if key in workflow and not isinstance(workflow[key], str):
            errors.append(f"workflow.{key} must be a string")
    for key in ("test_variables", "context_variables"):
        if key in workflow and not isinstance(workflow[key], dict):
            errors.append(f"workflow.{key} must be an object")
    if "agent_config" in workflow:
        errors.append("workflow.agent_config: omit it; Freya and freya.py set the required event model")
    for key in ("model", "llmConfig", "assistantLlmOverride", "routingLlmOverride", "extractionLlmOverride"):
        if key in workflow:
            errors.append(f"workflow.{key}: remove the model override; use the fixed model in freya.py")

    expected_urls = {}
    for path, methods in openapi["paths"].items():
        if not isinstance(methods, dict):
            continue
        for method in methods:
            if method.lower() not in {"get", "post", "put", "patch", "delete"}:
                continue
            try:
                url = operation_url(openapi, path, method)
            except ValueError as exc:
                errors.append(f"openapi: {exc}")
                continue
            if url in expected_urls and expected_urls[url] != (path, method.lower()):
                errors.append(f"openapi: URL {url!r} maps to multiple operations")
            expected_urls[url] = (path, method.lower())
    if not expected_urls:
        errors.append("openapi.servers and paths must define absolute tool URLs")
    identity_operations = [operation for path, methods in openapi["paths"].items()
                           if isinstance(methods, dict)
                           for method, operation in methods.items()
                           if method.lower() in {"get", "post", "put", "patch", "delete"}
                           and isinstance(operation, dict)
                           and (operation.get("operationId") == "verify_identity" or
                                not operation.get("operationId") and path.rstrip("/").split("/")[-1] == "verify_identity")]
    identity_complete_variable = None
    identity_response_names: set[str] = set()
    if len(identity_operations) == 1:
        try:
            identity_semantics = identity_response_fields(identity_operations[0])
            identity_complete_variable = "verify_identity_response.data." + identity_semantics["complete"]
            for code, response in (identity_operations[0].get("responses") or {}).items():
                if str(code).isdigit() and 200 <= int(code) < 300 and isinstance(response, dict):
                    schema = (response.get("content") or {}).get("application/json", {}).get("schema") or {}
                    identity_response_names.update((schema.get("properties") or {}).keys())
        except ValueError as exc:
            errors.append(f"verify_identity OpenAPI: {exc}")
    tool_locations: dict[str, set[str]] = {}

    def check_mapping(mapping, location: str) -> None:
        if not isinstance(mapping, list):
            errors.append(f"{location} must be an array of path/variable objects")
            return
        for i, item in enumerate(mapping):
            where = f"{location}[{i}]"
            if not isinstance(item, dict):
                errors.append(f"{where} must be an object with path and variable")
                continue
            path, variable = item.get("path"), item.get("variable")
            if not isinstance(path, str) or not path.startswith("data.") or len(path) <= 5:
                errors.append(f"{where}.path must start with 'data.' and name a response field")
            if not isinstance(variable, str) or not variable:
                errors.append(f"{where}.variable must be a nonempty string")

    def check_python(source, location: str) -> None:
        if not isinstance(source, str):
            errors.append(f"{location} must be a Python source string")
            return
        if not source.strip():
            return
        if "{{" in source or "}}" in source:
            errors.append(f"{location}: avoid {{{{ or }}}} in Python; read workflow values from vars")
        try:
            compile(source, location, "exec")
        except SyntaxError as exc:
            errors.append(f"{location}: Python syntax error on line {exc.lineno}: {exc.msg}")

    def check_tool(tool, location: str, *, placement="tool") -> None:
        if not isinstance(tool, dict) or tool.get("type") != "function":
            errors.append(f"{location}: expected a function tool object")
            return
        fn = tool.get("function")
        if not isinstance(fn, dict):
            errors.append(f"{location}.function must be an object")
            return
        name, handler = fn.get("name"), fn.get("handler")
        if not isinstance(name, str) or not name:
            errors.append(f"{location}.function.name must be a nonempty string")
            return
        tool_locations.setdefault(name, set()).add(location)
        if "response_mapping" in tool:
            check_mapping(tool["response_mapping"], f"{location}.response_mapping")
            if placement == "pre_action" and tool["response_mapping"]:
                errors.append(f"{location}.response_mapping is ignored; move it to the node's response_mapping")
            elif placement == "lifecycle" and tool["response_mapping"]:
                errors.append(f"{location}.response_mapping is ignored at end_of_call; remove it")
        for hook in ("pre_process_code", "post_process_code"):
            if hook in tool:
                check_python(tool[hook], f"{location}.{hook}")
                if handler != "__function__:api_call" and tool[hook]:
                    errors.append(f"{location}.{hook} is stripped from {handler} tools")
            if hook in fn:
                check_python(fn[hook], f"{location}.function.{hook}")
                errors.append(f"{location}.function.{hook} is ignored; move it to the tool object")
        if placement == "terminal":
            if handler != "__function__:end_call":
                errors.append(f"{location}: end_call terminal must use __function__:end_call")
            return
        if handler == "__function__:code_exec":
            params = fn.get("parameters")
            props = params.get("properties") if isinstance(params, dict) else None
            code = props.get("code") if isinstance(props, dict) else None
            source = code.get("default") if isinstance(code, dict) else None
            if source is not None:
                check_python(source, f"{location}.function.parameters.properties.code.default")
            elif placement == "pre_action":
                errors.append(f"{location}: code_exec pre_actions need a baked code.default Python string")
            return
        if handler == "__function__:end_call":
            errors.append(f"{location}: put end_call in an end_call node's terminal")
            return
        if handler != "__function__:api_call":
            errors.append(f"{location}: unsupported handler {handler!r}; use bank API or code_exec")
            return
        if name not in _BANK_TOOLS:
            errors.append(f"{location}: API tool {name!r} must use one of the seven bank tool names in OpenAPI")
        params = fn.get("parameters")
        props = params.get("properties") if isinstance(params, dict) else None
        if not isinstance(props, dict):
            errors.append(f"{location}.function.parameters.properties must be an object")
            return
        url_spec, method_spec = props.get("url"), props.get("method")
        url = url_spec.get("default") if isinstance(url_spec, dict) else None
        method = method_spec.get("default") if isinstance(method_spec, dict) else None
        if not isinstance(url, str) or url not in expected_urls:
            errors.append(f"{location}: url.default must be an exact URL from openapi.servers + paths")
            return
        api_path, api_method = expected_urls[url]
        if not isinstance(method, str) or method.lower() != api_method:
            errors.append(f"{location}: method.default must be {api_method.upper()} for {api_path}")
        operation = openapi["paths"][api_path][api_method]
        expected_name = operation.get("operationId") or api_path.rstrip("/").split("/")[-1]
        if name != expected_name:
            errors.append(f"{location}: function.name {name!r} must match OpenAPI operation {expected_name!r} at {api_path!r}")
        if name in {"add_note", "transfer_to_agent", "schedule_callback"}:
            try:
                flag = write_success_field(operation, name)
            except ValueError as exc:
                errors.append(f"{location}: {exc}")
            else:
                hook = tool.get("post_process_code")
                if hook is not None and (not isinstance(hook, str) or
                                         f"data.get({flag!r}) is True" not in hook):
                    errors.append(f"{location}.post_process_code: bind OpenAPI write-success field {flag!r}")
        try:
            expected_headers = required_header_bindings(openapi, api_path, api_method)
        except ValueError as exc:
            errors.append(f"{location}: {exc}")
        else:
            header_spec = props.get("headers")
            header_props = header_spec.get("properties") if isinstance(header_spec, dict) else None
            header_props = header_props if isinstance(header_props, dict) else {}
            for header_name, expected in expected_headers.items():
                matches = [leaf for key, leaf in header_props.items()
                           if isinstance(key, str) and key.lower() == header_name.lower()]
                if len(matches) != 1 or not isinstance(matches[0], dict) or any(
                        matches[0].get(key) != value for key, value in expected.items()):
                    errors.append(f"{location}.headers.properties.{header_name}: bind required OpenAPI header to {expected!r}")
        schema = _schema_for(openapi, api_path, api_method)
        api_props = schema.get("properties", {})
        body = props.get("body")
        if not isinstance(body, dict) or not isinstance(body.get("properties"), dict):
            errors.append(f"{location}: declare body.type=object and body.properties with one leaf per API field")
            return
        if body.get("type") != "object" or "default" in body:
            errors.append(f"{location}.body: use type=object and leaf properties; remove body.default")
        body_props = body["properties"]
        required = body.get("required", [])
        if not isinstance(required, list) or any(not isinstance(x, str) for x in required):
            errors.append(f"{location}.body.required must be a list of field names")
            required = []
        for field in schema.get("required", []):
            if field not in body_props:
                errors.append(f"{location}.body.properties: add required API field {field!r}")
            elif field not in required and isinstance(body_props[field], dict) and "default" not in body_props[field]:
                errors.append(f"{location}.body.required: include unbaked API field {field!r}")
        try:
            fixed_body_values = fixed_required_body_values(
                name, schema, provided={field: leaf.get("default") for field, leaf in body_props.items()
                                        if isinstance(leaf, dict) and "default" in leaf})
        except ValueError as exc:
            errors.append(f"{location}.body: {exc}")
        else:
            for field, value in fixed_body_values.items():
                leaf = body_props.get(field)
                if not isinstance(leaf, dict) or leaf.get("default") != value:
                    errors.append(f"{location}.body.properties.{field}: bind required fixed OpenAPI value {value!r}")
        try:
            customer_field = customer_identifier_field(schema)
        except ValueError as exc:
            errors.append(f"{location}: {exc}")
        else:
            customer_default = (body_props.get(customer_field) or {}).get("default")
            if customer_default != "{{customer_id}}":
                errors.append(f"{location}.body.properties.{customer_field}.default: "
                              "bind required customer identifier to {{customer_id}} from call variables")
        for field, spec in body_props.items():
            where = f"{location}.body.properties.{field}"
            if field not in api_props:
                errors.append(f"{where}: field is absent from the OpenAPI request body")
                continue
            if not isinstance(spec, dict):
                errors.append(f"{where}: expected a field schema object")
                continue
            expected = api_props[field]
            if spec.get("type") != expected.get("type"):
                errors.append(f"{where}.type: use {expected.get('type')!r} from OpenAPI")
            allowed = expected.get("enum")
            if allowed:
                chosen = spec.get("enum")
                default = spec.get("default")
                if chosen is None and ("default" not in spec or isinstance(default, str) and default.startswith("{{")):
                    errors.append(f"{where}.enum: constrain model-filled values to {allowed!r}")
                elif chosen is not None and (not isinstance(chosen, list) or not chosen or set(map(str, chosen)) - set(map(str, allowed))):
                    errors.append(f"{where}.enum: values must be a nonempty subset of {allowed!r}")
                if isinstance(default, str) and not default.startswith("{{") and default not in allowed:
                    errors.append(f"{where}.default: {default!r} is not in OpenAPI enum {allowed!r}")
            if "const" in spec and "default" not in spec:
                errors.append(f"{where}: const does not send a value; use default or let the model fill it")
        if name == "verify_identity":
            try:
                selector, choices = identity_selector_field(schema)
            except ValueError as exc:
                errors.append(f"{location}: {exc}")
            else:
                chosen = (body_props.get(selector) or {}).get("default")
                if chosen not in choices and chosen != "{{" + selector + "}}":
                    errors.append(f"{location}.body.properties.{selector}.default: "
                                  f"bind a valid security selector from {choices!r}")
        if name == "create_promise" and placement == "pre_action":
            try:
                semantic_fields = promise_request_fields(schema)
            except ValueError as exc:
                errors.append(f"{location}: {exc}")
            else:
                for role, variable in (("product", "pending_product_id"),
                                       ("amount", "pending_amount"), ("date", "pending_date")):
                    field = semantic_fields[role]
                    default = (body_props.get(field) or {}).get("default")
                    if default != "{{" + variable + "}}":
                        errors.append(f"{location}.body.properties.{field}.default: bind {role} to {{{{{variable}}}}}")
        if name == "add_note" and placement == "pre_action":
            try:
                semantic_fields = note_request_fields(schema)
            except ValueError as exc:
                errors.append(f"{location}: {exc}")
            else:
                for role, variable in (("product", "pending_note_product_id"),
                                       ("reason", "pending_note_reason")):
                    field = semantic_fields[role]
                    default = (body_props.get(field) or {}).get("default")
                    if default != "{{" + variable + "}}":
                        errors.append(f"{location}.body.properties.{field}.default: bind {role} to {{{{{variable}}}}}")
        if name == "transfer_to_agent" and placement == "pre_action" and ".nodes.transfer_" in location:
            try:
                field = transfer_request_fields(schema)["queue"]
            except ValueError as exc:
                errors.append(f"{location}: {exc}")
            else:
                default = (body_props.get(field) or {}).get("default")
                if default == "{{" + field + "}}":
                    errors.append(f"{location}.body.properties.{field}.default: bind transfer queue to a source-derived value")
        if name == "close_call" and placement == "pre_action" and ".nodes.close_call." in location:
            try:
                fields = close_request_fields(schema)
            except ValueError as exc:
                errors.append(f"{location}: {exc}")
            else:
                for role, variable in (("outcome", "close_outcome"), ("reason", "close_reason")):
                    field = fields[role]
                    if field and (body_props.get(field) or {}).get("default") != "{{" + variable + "}}":
                        errors.append(f"{location}.body.properties.{field}.default: bind {role} to {{{{{variable}}}}}")

    edges: dict[str, list[tuple[str, bool, bool]]] = {}
    for name, node in all_nodes.items():
        loc = f"workflow.{'global_nodes' if name in globals_ else 'nodes'}.{name}"
        if not isinstance(node, dict):
            errors.append(f"{loc} must be an object")
            continue
        kind = node.get("node_type", "conversation" if name in globals_ else None)
        if not isinstance(kind, str) or kind not in _NODE_TYPES:
            errors.append(f"{loc}.node_type: use conversation, api_request, code, or end_call")
        if node.get("name") != name:
            errors.append(f"{loc}.name must equal its node key {name!r}")
        if "agent_config" in node or "llmConfig" in node or "model" in node:
            errors.append(f"{loc}: remove model settings; the event model is fixed")
        for hook in ("pre_process_code", "post_process_code"):
            if hook in node:
                check_python(node[hook], f"{loc}.{hook}")
                errors.append(f"{loc}.{hook} is ignored; move it to the API tool object")
        tools, actions = node.get("tools", []), node.get("pre_actions", [])
        if not isinstance(tools, list):
            errors.append(f"{loc}.tools must be an array")
            tools = []
        if not isinstance(actions, list):
            errors.append(f"{loc}.pre_actions must be an array")
            actions = []
        if tools and kind != "conversation":
            errors.append(f"{loc}.tools are stripped from {kind} nodes; move them to a conversation node")
        if actions and kind not in {"api_request", "code"}:
            errors.append(f"{loc}.pre_actions are stripped from {kind} nodes; use an api_request or code node")
        task_message = node.get("task_message", "")
        raw_transitions = node.get("transitions", [])
        callable_transitions = raw_transitions if isinstance(raw_transitions, list) else []
        if name in {"add_note_call", "retry_note_call"}:
            matches = [(path, method, operation) for path, path_item in (openapi.get("paths") or {}).items()
                       for method, operation in path_item.items()
                       if isinstance(operation, dict) and
                       (operation.get("operationId") or path.rstrip("/").split("/")[-1]) == "add_note"]
            if len(matches) == 1:
                try:
                    flag = write_success_field(matches[0][2], "add_note")
                except ValueError as exc:
                    errors.append(f"{loc}: {exc}")
                else:
                    expected_variable = name + "_response.data." + flag
                    successes = [edge for edge in callable_transitions if isinstance(edge, dict)
                                 and edge.get("target_node") == "advance_note"]
                    def uses_result_flag(condition) -> bool:
                        if not isinstance(condition, dict):
                            return False
                        children = condition.get("conditions")
                        if isinstance(children, list):
                            return bool(children) and all(uses_result_flag(child) for child in children)
                        return (condition.get("variable") == expected_variable and
                                condition.get("operator") == "equals" and
                                str(condition.get("operand", "")).lower() == "true")
                    if len(successes) != 1 or not uses_result_flag(successes[0].get("condition")):
                        errors.append(f"{loc}.transitions: bind OpenAPI write-success field {flag!r}")
        if (kind == "conversation" and isinstance(task_message, str) and
                re.search(r"\bno\s+tool\s+calls?\s+(?:is|are)\s+allowed\b", task_message, re.I) and
                (tools or any(isinstance(t, dict) and t.get("is_tool_call") for t in callable_transitions))):
            errors.append(f"{loc}.task_message: 'no tool call is allowed' contradicts callable tools or is_tool_call transitions")
        for i, tool in enumerate(tools):
            check_tool(tool, f"{loc}.tools[{i}]")
        for i, tool in enumerate(actions):
            check_tool(tool, f"{loc}.pre_actions[{i}]", placement="pre_action")
        if "response_mapping" in node:
            check_mapping(node["response_mapping"], f"{loc}.response_mapping")
            if kind not in {"api_request", "code"} and node["response_mapping"]:
                errors.append(f"{loc}.response_mapping is ignored on {kind} nodes; move it to a conversation tool or an action node")
            elif not actions and node["response_mapping"]:
                errors.append(f"{loc}.response_mapping has no pre_actions result to map")
            elif (node["response_mapping"] and actions and
                  all(isinstance(action, dict) and isinstance(action.get("function"), dict) and
                      action["function"].get("handler") == "__function__:code_exec" for action in actions)):
                errors.append(f"{loc}.response_mapping cannot read code_exec output data; use vars in the code snippet")
        if name == "load_account" and actions:
            action = actions[0] if isinstance(actions[0], dict) else {}
            fn = action.get("function") if isinstance(action.get("function"), dict) else {}
            params = fn.get("parameters") if isinstance(fn.get("parameters"), dict) else {}
            props = params.get("properties") if isinstance(params.get("properties"), dict) else {}
            url_spec = props.get("url") if isinstance(props.get("url"), dict) else {}
            url = url_spec.get("default")
            if fn.get("name") == "get_account" and url in expected_urls:
                api_path, api_method = expected_urls[url]
                operation = openapi["paths"][api_path][api_method]
                try:
                    collection, _ = account_response_collection(operation)
                except ValueError as exc:
                    errors.append(f"{loc}: {exc}")
                else:
                    expected_mapping = {"path": "data." + collection, "variable": "account_products"}
                    mapping = node.get("response_mapping")
                    if not isinstance(mapping, list) or expected_mapping not in mapping:
                        errors.append(f"{loc}.response_mapping: map {expected_mapping['path']} to account_products")
        if kind == "end_call":
            if node.get("transitions"):
                errors.append(f"{loc}: end_call nodes must not have outgoing transitions")
            check_tool(node.get("terminal"), f"{loc}.terminal", placement="terminal")
            on_enter = node.get("on_enter", {})
            if isinstance(on_enter, dict) and on_enter.get("type") not in (None, "silent"):
                errors.append(f"{loc}.on_enter: put the farewell in terminal.spoken_message, not on_enter")
        transitions = node.get("transitions", [])
        if not isinstance(transitions, list):
            errors.append(f"{loc}.transitions must be an array")
            transitions = []
        edges[name] = []
        for i, transition in enumerate(transitions):
            tloc = f"{loc}.transitions[{i}]"
            if not isinstance(transition, dict):
                errors.append(f"{tloc} must be an object")
                continue
            target = transition.get("target_node")
            if not isinstance(target, str) or (target not in all_nodes and not (name in globals_ and target == "__previous__")):
                errors.append(f"{tloc}.target_node {target!r} does not name a node")
                continue
            if target != "__previous__":
                condition = transition.get("condition")
                for variable in _condition_variables(condition):
                    prefix = "verify_identity_response.data."
                    if variable.startswith(prefix) and identity_response_names and variable[len(prefix):] not in identity_response_names:
                        errors.append(f"{tloc}: {variable} is absent from verify_identity OpenAPI response")
                edges[name].append((target, _condition_has_verification(condition, identity_complete_variable),
                                    _condition_has_confirmation(condition)))
        for field in ("passthrough_default_transition", "max_turn_transition"):
            target = node.get(field)
            if target is not None:
                if not isinstance(target, str) or (target not in all_nodes and not (name in globals_ and target == "__previous__")):
                    errors.append(f"{loc}.{field} {target!r} does not name a node")
                elif target != "__previous__" and (field != "passthrough_default_transition" or node.get("passthrough")):
                    edges[name].append((target, False, False))
        if node.get("passthrough"):
            if kind == "end_call":
                errors.append(f"{loc}: end_call nodes must not be passthrough")
            if not transitions or not isinstance(transitions[-1], dict) or transitions[-1].get("condition") != {"operator": "always"}:
                errors.append(f"{loc}.transitions: finish a passthrough node with an always transition")
            if not node.get("passthrough_default_transition"):
                errors.append(f"{loc}.passthrough_default_transition: name the fallback node")
        if name in globals_:
            excluded = node.get("excluded_nodes", [])
            if not isinstance(excluded, list):
                errors.append(f"{loc}.excluded_nodes must be an array")
            else:
                for excluded_node in excluded:
                    if not isinstance(excluded_node, str) or excluded_node not in all_nodes:
                        errors.append(f"{loc}.excluded_nodes: {excluded_node!r} does not name a node")

    lifecycle = workflow.get("tools", [])
    if not isinstance(lifecycle, list):
        errors.append("workflow.tools must be an array")
        lifecycle = []
    for i, tool in enumerate(lifecycle):
        check_tool(tool, f"workflow.tools[{i}]", placement="lifecycle")
        config = tool.get("config") if isinstance(tool, dict) else None
        if isinstance(tool, dict) and (not isinstance(config, dict) or not isinstance(config.get("alwaysRunsAt"), list) or "end_of_call" not in config["alwaysRunsAt"]):
            errors.append(f"workflow.tools[{i}]: lifecycle bank tools need config.alwaysRunsAt containing end_of_call")

    if initial in nodes:
        reachable = set()
        queue = deque([initial])
        while queue:
            current = queue.popleft()
            if current in reachable:
                continue
            reachable.add(current)
            queue.extend(target for target, _, _ in edges.get(current, []))
        for name in nodes:
            if name not in reachable:
                errors.append(f"workflow.nodes.{name}: unreachable from initial_node")
        end_nodes = {n for n in reachable if isinstance(all_nodes.get(n), dict) and all_nodes[n].get("node_type") == "end_call"}
        if not end_nodes:
            errors.append("workflow: no reachable end_call node; add a terminal path")
        reverse = {n: [] for n in all_nodes}
        for source, links in edges.items():
            for target, _, _ in links:
                reverse[target].append(source)
        can_end = set(end_nodes)
        queue = deque(end_nodes)
        while queue:
            for parent in reverse.get(queue.popleft(), []):
                if parent not in can_end:
                    can_end.add(parent)
                    queue.append(parent)
        for name in reachable - can_end:
            errors.append(f"workflow.nodes.{name}: no route to an end_call node")

        # A correction turn's max-turn route must actually leave the correction
        # loop. Follow only automatic fallback paths, stopping at a fresh caller
        # turn; conditional branches can depend on newly supplied valid terms.
        def automatic_route_reenters_correction(start: str) -> bool:
            pending, visited = [start], set()
            while pending:
                current = pending.pop()
                if current in visited:
                    continue
                visited.add(current)
                node = all_nodes.get(current)
                if not isinstance(node, dict):
                    continue
                if node.get("node_type") == "conversation":
                    if re.search(r"(?:^|_)correction(?:_|$)", current):
                        return True
                    if not node.get("passthrough"):
                        continue
                if node.get("node_type") == "end_call":
                    continue
                transitions = node.get("transitions", [])
                if isinstance(transitions, list):
                    pending.extend(transition.get("target_node") for transition in transitions
                                   if isinstance(transition, dict) and
                                   (not transition.get("condition") or
                                    transition["condition"] == {"operator": "always"}))
                if node.get("passthrough"):
                    pending.append(node.get("passthrough_default_transition"))
            return False

        for name in reachable:
            node = all_nodes.get(name)
            if (not isinstance(node, dict) or node.get("node_type") != "conversation"
                    or not re.search(r"(?:^|_)correction(?:_|$)", name)):
                continue
            max_target = node.get("max_turn_transition")
            if isinstance(max_target, str) and automatic_route_reenters_correction(max_target):
                errors.append(f"workflow.nodes.{name}.max_turn_transition: max-turn correction route re-enters a correction turn without a fresh non-correction conversation or end_call")

        # Some policies explicitly forbid notes once a transfer is made.
        # Check every graph path after the transfer, including passthrough
        # defaults, because a later note action would violate that rule.
        if re.search(r"\bon any transfer\b.{0,100}\b(?:add no|no)\b.{0,30}\bnotes\b",
                     str(workflow.get("system_prompt", "")), re.I | re.S):
            for name in reachable:
                node = all_nodes.get(name)
                if not isinstance(node, dict):
                    continue
                actions = node.get("pre_actions", [])
                if not isinstance(actions, list) or not any(
                        _tool_name(action) == "transfer_to_agent" for action in actions):
                    continue
                pending, visited = [name], set()
                while pending:
                    current = pending.pop()
                    if current in visited:
                        continue
                    visited.add(current)
                    downstream = all_nodes.get(current)
                    if not isinstance(downstream, dict):
                        continue
                    calls = []
                    for field in ("pre_actions", "tools"):
                        items = downstream.get(field, [])
                        if isinstance(items, list):
                            calls.extend(items)
                    if any(_tool_name(action) == "add_note" for action in calls):
                        errors.append(f"workflow.nodes.{name}: add_note is reachable after transfer_to_agent despite the source policy forbidding notes on transfer")
                        break
                    pending.extend(target for target, _, _ in edges.get(current, []))

        # A policy requiring one combined read-back needs a deterministic
        # all-products-disposed route before entering its confirmation turn.
        # The reducer's multi_next=confirm branch is the graph contract; the
        # arithmetic and product order inside that reducer are covered by
        # executable generator tests.
        if re.search(r"read all promises back together.{0,100}one confirmation|combined confirmation",
                     str(workflow.get("system_prompt", "")), re.I | re.S):
            mentioned_counts = [int(value["operand"]) for _, value in _walk(workflow)
                                if isinstance(value, dict) and value.get("variable") == "product_count"
                                and str(value.get("operand", "")).isdigit()]
            count_domain = frozenset(range(max([2, *mentioned_counts]) + 2))

            def possible_product_counts(condition) -> frozenset[int]:
                counts = count_domain
                if not isinstance(condition, dict):
                    return counts
                children = condition.get("conditions")
                if isinstance(children, list) and children:
                    groups = [possible_product_counts(child) for child in children]
                    if condition.get("logic") == "and":
                        return frozenset.intersection(*groups)
                    if condition.get("logic") == "or":
                        return frozenset.union(*groups)
                    return counts
                if condition.get("variable") != "product_count":
                    return counts
                try:
                    operand = int(condition.get("operand"))
                except (TypeError, ValueError):
                    return counts
                operator = condition.get("operator")
                if operator == "equals":
                    return frozenset(n for n in counts if n == operand)
                if operator == "greater_than":
                    return frozenset(n for n in counts if n > operand)
                if operator == "less_than":
                    return frozenset(n for n in counts if n < operand)
                return counts

            confirmation_turns = {
                name for name, node in all_nodes.items()
                if isinstance(node, dict) and node.get("node_type") == "conversation"
                and isinstance(node.get("on_enter"), dict)
                and node["on_enter"].get("type") == "tts"
                and isinstance(node.get("extraction_schema"), list)
                and any(isinstance(item, dict) and "confirm" in str(item.get("name", "")).lower()
                        for item in node["extraction_schema"])
            }
            seen_multi = set()
            pending = deque([(initial, False, count_domain)])
            while pending:
                current, disposed, possible_counts = pending.popleft()
                if (current, disposed, possible_counts) in seen_multi:
                    continue
                seen_multi.add((current, disposed, possible_counts))
                if current in confirmation_turns and not disposed and any(n > 1 for n in possible_counts):
                    errors.append(f"workflow.nodes.{current}: combined confirmation is reachable before multi_next=confirm resolves every product")
                node = all_nodes.get(current)
                if not isinstance(node, dict):
                    continue
                transitions = node.get("transitions", [])
                for transition in transitions if isinstance(transitions, list) else []:
                    if not isinstance(transition, dict):
                        continue
                    target = transition.get("target_node")
                    if target in all_nodes:
                        allowed = possible_counts & possible_product_counts(transition.get("condition"))
                        if not allowed:
                            continue
                        gated = _condition_requires(transition.get("condition"),
                                                    "multi_next", "equals", "confirm")
                        pending.append((target, disposed or gated, allowed))
                for field in ("passthrough_default_transition", "max_turn_transition"):
                    target = node.get(field)
                    if target in all_nodes and (field != "passthrough_default_transition" or node.get("passthrough")):
                        pending.append((target, disposed, possible_counts))

        def safe_code_failure_route(start: str) -> bool:
            """A failed computation may only reach fixed speech and closing actions."""
            pending, visited = [start], set()
            while pending:
                current = pending.pop()
                if current in visited:
                    continue
                visited.add(current)
                target_node = all_nodes.get(current)
                if not isinstance(target_node, dict):
                    return False
                if target_node.get("node_type") == "conversation":
                    on_enter = target_node.get("on_enter")
                    if not (target_node.get("passthrough") and isinstance(on_enter, dict)
                            and on_enter.get("type") == "tts"):
                        return False
                pending.extend(target for target, _, _ in edges.get(current, []))
            return bool(visited & end_nodes)

        def safe_ambiguous_write_route(start: str) -> bool:
            """An unknown write result must reach closure without another write or success claim."""
            pending, visited = [start], set()
            while pending:
                current = pending.pop()
                if current in visited:
                    continue
                visited.add(current)
                target_node = all_nodes.get(current)
                if not isinstance(target_node, dict) or _promise_success_notice(target_node):
                    return False
                actions = target_node.get("pre_actions", [])
                if isinstance(actions, list) and any(
                        _tool_name(action) == "create_promise" for action in actions):
                    return False
                if target_node.get("node_type") == "conversation":
                    on_enter = target_node.get("on_enter")
                    if not (target_node.get("passthrough") and isinstance(on_enter, dict)
                            and on_enter.get("type") == "tts"):
                        return False
                pending.extend(target for target, _, _ in edges.get(current, []))
            return bool(visited & end_nodes)

        def reaches_success_notice(start: str) -> bool:
            """Stop at another bank write or free-form turn before its own result."""
            pending, visited = [start], set()
            while pending:
                current = pending.pop()
                if current in visited:
                    continue
                visited.add(current)
                target_node = all_nodes.get(current)
                if not isinstance(target_node, dict):
                    continue
                if _promise_success_notice(target_node):
                    return True
                actions = target_node.get("pre_actions", [])
                if isinstance(actions, list) and any(
                        _tool_name(action) == "create_promise" for action in actions):
                    continue
                if target_node.get("node_type") == "conversation":
                    continue
                pending.extend(target for target, _, _ in edges.get(current, []))
            return False

        def reaches_repeat_promise(start: str) -> bool:
            """Find another promise action before a success notice or new caller turn."""
            pending, visited = [start], set()
            while pending:
                current = pending.pop()
                if current in visited:
                    continue
                visited.add(current)
                target_node = all_nodes.get(current)
                if not isinstance(target_node, dict) or _promise_success_notice(target_node):
                    continue
                actions = target_node.get("pre_actions", [])
                if isinstance(actions, list) and any(
                        _tool_name(action) == "create_promise" for action in actions):
                    return True
                if target_node.get("node_type") == "conversation":
                    continue
                pending.extend(target for target, _, _ in edges.get(current, []))
            return False

        def promise_response_contract(action: dict) -> tuple[str, set[str], set[str], set[str]]:
            """Read confirmed and nonrecording response shapes from the supplied API."""
            fn = action.get("function", {})
            props = fn.get("parameters", {}).get("properties", {})
            url = props.get("url", {}).get("default")
            if url not in expected_urls:
                return "", set(), set(), set()
            path, method = expected_urls[url]
            responses = openapi["paths"][path][method].get("responses", {})
            success_codes, false_codes, no_write_codes = set(), set(), set()
            boolean_fields: set[str] = set()
            for status, response in responses.items():
                if not isinstance(response, dict):
                    continue
                schema = response.get("content", {}).get("application/json", {}).get("schema", {})
                fields = schema.get("properties", {}) if isinstance(schema, dict) else {}
                boolean_fields.update(key for key, spec in fields.items()
                                      if isinstance(spec, dict) and spec.get("type") == "boolean")
            flag = "ok" if "ok" in boolean_fields else next(iter(boolean_fields)) if len(boolean_fields) == 1 else ""
            for status, response in responses.items():
                if not isinstance(response, dict) or not str(status).isdigit():
                    continue
                description = str(response.get("description", "")).lower()
                schema = response.get("content", {}).get("application/json", {}).get("schema", {})
                fields = schema.get("properties", {}) if isinstance(schema, dict) else {}
                if flag and isinstance(fields, dict) and flag in fields and 200 <= int(status) < 300:
                    success_codes.add(str(status))
                    if "false" in description and _source_guarantees_no_write(description):
                        false_codes.add(str(status))
                if not 200 <= int(status) < 300 and _source_guarantees_no_write(description):
                    no_write_codes.add(str(status))
            return flag, success_codes, false_codes, no_write_codes

        def promise_response_kind(name: str, action: dict, condition) -> str:
            flag, success_codes, false_codes, no_write_codes = promise_response_contract(action)
            status_var = f"{name}_response.status_code"
            flag_var = f"{name}_response.data.{flag}"
            clauses = _condition_clauses(condition)
            if not clauses:
                return "unknown"
            kinds = set()
            for clause in clauses:
                status_values = {value for variable, operator, value in clause
                                 if variable == status_var and operator == "equals"}
                true_flag = (flag_var, "equals", "true") in clause if flag else False
                false_flag = (flag_var, "equals", "false") in clause if flag else False
                if status_values and status_values <= success_codes and true_flag:
                    kinds.add("success")
                elif (status_values and status_values <= no_write_codes or
                      status_values and status_values <= false_codes and false_flag):
                    kinds.add("nonrecording")
                else:
                    kinds.add("unknown")
            return next(iter(kinds)) if len(kinds) == 1 else "unknown"

        # The first promise action may retry only after a bank response that
        # its OpenAPI contract says recorded nothing. A tool failure alone is
        # ambiguous because the bank may have committed before Freya errored.
        for name in reachable:
            node = all_nodes.get(name)
            if not isinstance(node, dict):
                continue
            actions = node.get("pre_actions", [])
            promise_actions = [action for action in actions if _tool_name(action) == "create_promise"] if isinstance(actions, list) else []
            if not promise_actions:
                continue
            transitions = node.get("transitions", [])
            for i, transition in enumerate(transitions if isinstance(transitions, list) else []):
                if not isinstance(transition, dict):
                    continue
                target = transition.get("target_node")
                if target not in all_nodes:
                    continue
                kind = promise_response_kind(name, promise_actions[0], transition.get("condition"))
                location = f"workflow.nodes.{name}.transitions[{i}]"
                if reaches_repeat_promise(target) and kind != "nonrecording":
                    errors.append(f"{location}: create_promise retry requires an explicit bank nonrecording response")
                if reaches_success_notice(target) and kind != "success":
                    errors.append(f"{location}: recorded-promise notice requires an explicit successful bank response")
                if kind == "unknown" and not safe_ambiguous_write_route(target):
                    errors.append(f"{location}: ambiguous create_promise result must close without retry or success speech")
            default = node.get("passthrough_default_transition") if node.get("passthrough") else None
            if not isinstance(default, str) or not safe_ambiguous_write_route(default):
                errors.append(f"workflow.nodes.{name}.passthrough_default_transition: ambiguous create_promise result must close without retry or success speech")

        def reaches_promise_stage(start: str) -> bool:
            """Follow deterministic steps up to the next spoken turn."""
            pending, visited = [start], set()
            while pending:
                current = pending.pop()
                if current in visited:
                    continue
                visited.add(current)
                target_node = all_nodes.get(current)
                if not isinstance(target_node, dict):
                    continue
                actions = target_node.get("pre_actions", [])
                if isinstance(actions, list) and any(
                        _tool_name(action) == "create_promise" for action in actions):
                    return True
                if target_node.get("node_type") == "conversation":
                    on_enter = target_node.get("on_enter")
                    extractions = target_node.get("extraction_schema", [])
                    if (isinstance(on_enter, dict) and on_enter.get("type") == "tts" and
                            isinstance(extractions, list) and any(
                                isinstance(item, dict) and
                                "confirm" in str(item.get("name", "")).lower()
                                for item in extractions)):
                        return True
                    continue
                pending.extend(target for target, _, _ in edges.get(current, []))
            return False

        # Freya can leave a code node after code_exec fails. A stale value must
        # never send the call back to free-form discussion or onward to a write.
        for name in reachable:
            node = all_nodes.get(name)
            if not isinstance(node, dict):
                continue
            actions = node.get("pre_actions", [])
            if not isinstance(actions, list) or not any(
                    isinstance(action, dict) and isinstance(action.get("function"), dict) and
                    action["function"].get("handler") == "__function__:code_exec"
                    for action in actions):
                continue
            transitions = node.get("transitions", [])
            if not isinstance(transitions, list):
                continue
            if not any(reaches_promise_stage(target) for target, _, _ in edges.get(name, [])):
                continue
            loc = f"workflow.nodes.{name}"
            failure_variable = f"{name}_failure_count"
            first = transitions[0] if transitions else None
            if not (isinstance(first, dict) and
                    _condition_requires(first.get("condition"), failure_variable, "greater_than", "0") and
                    isinstance(first.get("target_node"), str) and
                    safe_code_failure_route(first["target_node"])):
                errors.append(f"{loc}: code_exec confirmation/promise gate needs a first {failure_variable} > 0 route to fixed failure speech and close")
            default = node.get("passthrough_default_transition") if node.get("passthrough") else None
            if not isinstance(default, str) or not safe_code_failure_route(default):
                errors.append(f"{loc}.passthrough_default_transition: code_exec failure fallback must lead to fixed failure speech and close, not ungated conversation")

        # Propagate whether identity has been proven. A path to an account tool
        # with state=False is an actionable potential policy breach.
        seen = set()
        queue = deque([(initial, False)])
        while queue:
            name, verified = queue.popleft()
            if (name, verified) in seen:
                continue
            seen.add((name, verified))
            node = all_nodes.get(name)
            if not isinstance(node, dict):
                continue
            calls = []
            for field in ("tools", "pre_actions"):
                if isinstance(node.get(field, []), list):
                    calls.extend(node.get(field, []))
            for tool in calls:
                if isinstance(tool, dict) and isinstance(tool.get("function"), dict):
                    tool_name = tool["function"].get("name")
                    if tool_name in _ACCOUNT_TOOLS and not verified:
                        errors.append(f"workflow.nodes.{name}: {tool_name} may run before a verified identity gate; route through a positive verification condition")
            for target, gate, _ in edges.get(name, []):
                queue.append((target, verified or gate))

        # A model-callable create_promise must not be available until a
        # deterministic positive confirmation condition has been crossed.
        # Prompt prose and is_tool_call transitions alone cannot prove this.
        confirmed_seen = set()
        queue = deque([(initial, False)])
        while queue:
            name, confirmed = queue.popleft()
            if (name, confirmed) in confirmed_seen:
                continue
            confirmed_seen.add((name, confirmed))
            node = all_nodes.get(name)
            if not isinstance(node, dict):
                continue
            callable_tools = node.get("tools", []) if isinstance(node.get("tools", []), list) else []
            entry_actions = node.get("pre_actions", []) if isinstance(node.get("pre_actions", []), list) else []
            if not confirmed and any(_tool_name(tool) == "create_promise"
                                     for tool in callable_tools + entry_actions):
                errors.append(f"workflow.nodes.{name}: create_promise is reachable without a positive confirmation condition; gate this route on a confirmed state variable")
            for target, _, gate in edges.get(name, []):
                queue.append((target, confirmed or gate))

        # A fixed recorded-promise notice requires a successful bank response from
        # create_promise on every path, including passthrough defaults.
        success_seen = set()
        queue = deque([(initial, False)])
        while queue:
            name, promise_ok = queue.popleft()
            if (name, promise_ok) in success_seen:
                continue
            success_seen.add((name, promise_ok))
            node = all_nodes.get(name)
            if not isinstance(node, dict):
                continue
            if _promise_success_notice(node) and not promise_ok:
                errors.append(f"workflow.nodes.{name}: recorded-promise success speech is reachable without an explicit successful create_promise response")
            actions = node.get("pre_actions", [])
            promise_action = next((action for action in actions if _tool_name(action) == "create_promise"), None) if isinstance(actions, list) else None
            has_promise_action = promise_action is not None
            for transition in node.get("transitions", []) if isinstance(node.get("transitions", []), list) else []:
                if not isinstance(transition, dict):
                    continue
                target = transition.get("target_node")
                if target not in all_nodes:
                    continue
                gated = (has_promise_action and promise_response_kind(
                    name, promise_action, transition.get("condition")) == "success")
                queue.append((target, gated if has_promise_action else promise_ok))
            for field in ("passthrough_default_transition", "max_turn_transition"):
                target = node.get(field)
                if target in all_nodes and (field != "passthrough_default_transition" or node.get("passthrough")):
                    queue.append((target, False if has_promise_action else promise_ok))

    closers = tool_locations.get("close_call", set())
    lifecycle_close = any(
        _tool_name(tool) == "close_call" and isinstance(tool.get("config"), dict)
        and isinstance(tool["config"].get("alwaysRunsAt"), list)
        and "end_of_call" in tool["config"]["alwaysRunsAt"]
        for tool in lifecycle if isinstance(tool, dict)
    )
    if not closers:
        errors.append("workflow: add close_call to record every call outcome")
    elif lifecycle_close and len(closers) > 1:
        errors.append("workflow: use either a lifecycle close_call or an in-flow close_call, not both; each call must close once")
    elif not lifecycle_close and initial in nodes:
        # An api_request pre_action runs on entry. A conversation tool is
        # optional, so it cannot alone prove that a path closes the ledger.
        seen_close = set()
        queue = deque([(initial, False)])
        while queue:
            name, closed = queue.popleft()
            if (name, closed) in seen_close:
                continue
            seen_close.add((name, closed))
            node = all_nodes.get(name)
            if not isinstance(node, dict):
                continue
            actions = node.get("pre_actions", [])
            if not isinstance(actions, list):
                actions = []
            names = [_tool_name(tool) for tool in actions]
            if "close_call" in names:
                if closed:
                    errors.append(f"workflow.nodes.{name}: close_call can run more than once on a loop")
                if any(item in _BANK_TOOLS for item in names[names.index("close_call") + 1:]):
                    errors.append(f"workflow.nodes.{name}: close_call must be the last bank API call")
                closed = True
            elif closed and any(item in _BANK_TOOLS for item in names):
                errors.append(f"workflow.nodes.{name}: a bank API call may run after close_call")
            if node.get("node_type") == "end_call" and not closed:
                errors.append(f"workflow.nodes.{name}: path reaches end_call without a preceding close_call pre_action")
            for target, _, _ in edges.get(name, []):
                queue.append((target, closed))
        if not any(
            isinstance(node, dict) and isinstance(node.get("pre_actions"), list)
            and any(_tool_name(tool) == "close_call" for tool in node["pre_actions"])
            for node in nodes.values()
        ):
            errors.append("workflow: put close_call in an api_request pre_actions node before end_call, or use an end_of_call lifecycle tool")
    if not tool_locations.get("verify_identity"):
        errors.append("workflow: add verify_identity before account access")
    if not tool_locations.get("get_account"):
        errors.append("workflow: add get_account after identity verification")
    return list(dict.fromkeys(errors))


def validate_generic_workflow(workflow: dict, openapi: dict) -> list[str]:
    """Validate a contract-driven workflow without assuming practice tool names."""
    errors: list[str] = []
    if not isinstance(workflow, dict):
        return ["workflow must be a JSON object"]
    try:
        contract = normalize_openapi(openapi)
    except ValueError as error:
        return [f"openapi: {error}"]
    operations = {}
    for path, methods in (contract.get("paths") or {}).items():
        if not isinstance(methods, dict):
            continue
        for method, operation in methods.items():
            if method.lower() not in {"get", "post", "put", "patch", "delete"} or not isinstance(operation, dict):
                continue
            try:
                url = operation_url(contract, path, method)
            except ValueError as error:
                errors.append(f"openapi: {error}")
                continue
            name = str(operation.get("operationId") or path.rstrip("/").split("/")[-1])
            if not name or url in operations:
                errors.append(f"openapi: ambiguous operation at {path}")
            operations[url] = (name, method.upper(), operation)
    if not operations:
        errors.append("openapi: no callable HTTP operations")
    nodes = workflow.get("nodes")
    if not isinstance(nodes, dict) or not nodes:
        return errors + ["workflow.nodes must be a nonempty object"]
    if len(operations) > 1 and "service" not in nodes:
        errors.append("workflow: contract-driven account service is unavailable because identity could not be proved")
    initial = workflow.get("initial_node")
    if initial not in nodes:
        errors.append("workflow.initial_node must name a node")
    for path, value in _walk(workflow):
        if value is None:
            errors.append(f"{path}: remove null")
        elif isinstance(value, str) and _SECRET.search(value):
            errors.append(f"{path}: remove embedded secret")
    edges = {}
    tool_names = set()
    tool_urls = set()
    for key, node in nodes.items():
        location = f"workflow.nodes.{key}"
        if not isinstance(node, dict):
            errors.append(f"{location} must be an object")
            continue
        if node.get("name") != key:
            errors.append(f"{location}.name must equal the node key")
        if node.get("node_type") not in _NODE_TYPES:
            errors.append(f"{location}.node_type is invalid")
        edges[key] = []
        for field in ("tools", "pre_actions"):
            for index, tool in enumerate(node.get(field) or []):
                where = f"{location}.{field}[{index}]"
                fn = tool.get("function") if isinstance(tool, dict) else None
                if not isinstance(fn, dict):
                    errors.append(f"{where}: expected a function tool")
                    continue
                handler = fn.get("handler")
                if handler == "__function__:code_exec":
                    source = (((fn.get("parameters") or {}).get("properties") or {}).get("code") or {}).get("default")
                    if not isinstance(source, str):
                        errors.append(f"{where}: code_exec needs source")
                    else:
                        try:
                            compile(source, where, "exec")
                        except SyntaxError as error:
                            errors.append(f"{where}: Python syntax error on line {error.lineno}")
                    continue
                if handler != "__function__:api_call":
                    errors.append(f"{where}: unsupported tool handler")
                    continue
                props = (fn.get("parameters") or {}).get("properties") or {}
                url = (props.get("url") or {}).get("default")
                method = (props.get("method") or {}).get("default")
                if url not in operations:
                    errors.append(f"{where}: URL is absent from the supplied OpenAPI")
                    continue
                expected_name, expected_method, operation = operations[url]
                if fn.get("name") != expected_name or method != expected_method:
                    errors.append(f"{where}: operation name or HTTP method differs from OpenAPI")
                tool_names.add(expected_name)
                tool_urls.add(url)
                schema = (operation.get("requestBody") or {}).get("content", {}).get("application/json", {}).get("schema") or {}
                body = props.get("body") or {}
                leaves = body.get("properties") or {}
                if not isinstance(leaves, dict):
                    errors.append(f"{where}: request body fields must be an object")
                    continue
                for required in schema.get("required") or []:
                    if required not in leaves:
                        errors.append(f"{where}: required request field {required!r} is absent")
                for body_field in leaves:
                    if body_field not in (schema.get("properties") or {}):
                        errors.append(f"{where}: request field {body_field!r} is absent from OpenAPI")
        for transition in node.get("transitions") or []:
            target = transition.get("target_node") if isinstance(transition, dict) else None
            if target not in nodes:
                errors.append(f"{location}: transition target {target!r} is absent")
            else:
                edges[key].append(target)
        for field in ("passthrough_default_transition", "max_turn_transition"):
            target = node.get(field)
            if target:
                if target not in nodes:
                    errors.append(f"{location}: {field} target is absent")
                else:
                    edges[key].append(target)
        if node.get("node_type") == "end_call":
            terminal = node.get("terminal") or {}
            if (terminal.get("function") or {}).get("handler") != "__function__:end_call":
                errors.append(f"{location}: end_call needs its terminal handler")
    if initial in nodes:
        reached, pending = set(), [initial]
        while pending:
            current = pending.pop()
            if current in reached:
                continue
            reached.add(current)
            pending.extend(edges.get(current, []))
        if not any(nodes[name].get("node_type") == "end_call" for name in reached if isinstance(nodes[name], dict)):
            errors.append("workflow: no reachable end_call node")
        for name in nodes.keys() - reached:
            errors.append(f"workflow.nodes.{name}: unreachable from initial_node")
    for url in sorted(operations.keys() - tool_urls):
        errors.append(f"workflow: callable contract operation {operations[url][0]!r} is not exposed")
    return errors


def run_practice(agent_id: str, brief: str, only=None, reps: int = 1,
                 style: str | None = None, timeout: float = 1800) -> dict:
    """Run the kit's official scorer once and capture its structured result.

    Call only when the workspace's single simulation slot is available.
    """
    if not isinstance(agent_id, str) or not agent_id:
        raise ValueError("agent_id must be a nonempty string")
    if not isinstance(brief, str) or not brief:
        raise ValueError("brief must be a nonempty string")
    if not isinstance(reps, int) or reps < 1:
        raise ValueError("reps must be a positive integer")
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    command = [sys.executable, str(ROOT / "tools" / "run_cases.py"), agent_id,
               "--brief", brief, "--reps", str(reps)]
    if only:
        command += ["--only", ",".join(only) if isinstance(only, (list, tuple)) else str(only)]
    if style:
        command += ["--style", style]
    with tempfile.TemporaryDirectory(prefix="btb-practice-") as directory:
        out = Path(directory) / "result.json"
        command += ["--out", str(out)]
        try:
            finished = subprocess.run(command, cwd=ROOT, capture_output=True, text=True,
                                      timeout=timeout, check=False)
        except subprocess.TimeoutExpired as exc:
            return {"ok": False, "timed_out": True, "stdout": exc.stdout or "",
                    "stderr": exc.stderr or "", "result": None}
        result = json.loads(out.read_text()) if out.exists() else None
        return {"ok": finished.returncode == 0 and result is not None,
                "returncode": finished.returncode, "stdout": finished.stdout,
                "stderr": finished.stderr, "result": result}
