"""Offline checks for a generated Freya workflow and a wrapper for practice runs.

The validator catches errors that the workflow API may silently strip. It is a
preflight check, not a substitute for the API dry run or a scored simulation.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from collections import deque
from pathlib import Path


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


def _condition_has_verification(condition) -> bool:
    """Recognize a positive, deterministic identity gate on an edge."""
    if not isinstance(condition, dict):
        return False
    children = condition.get("conditions")
    if isinstance(children, list):
        logic = condition.get("logic")
        if logic == "and":
            return any(_condition_has_verification(c) for c in children)
        if logic == "or":
            return bool(children) and all(_condition_has_verification(c) for c in children)
        return False
    variable = str(condition.get("variable", "")).lower()
    operand = str(condition.get("operand", "")).lower()
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

    servers = [s.get("url", "").rstrip("/") for s in openapi.get("servers", []) if isinstance(s, dict)]
    expected_urls = {
        f"{server}{path}": (path, method.lower())
        for server in servers
        for path, methods in openapi["paths"].items()
        if isinstance(methods, dict)
        for method in methods
        if method.lower() in {"get", "post", "put", "patch", "delete"}
    }
    if not expected_urls:
        errors.append("openapi.servers and paths must define absolute tool URLs")
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
        if name not in _BANK_TOOLS or f"/{name}" not in openapi["paths"]:
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
        if name != api_path.lstrip("/"):
            errors.append(f"{location}: function.name {name!r} must match API path {api_path!r}")
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
            if field == "customer_id" and spec.get("default") != "{{customer_id}}":
                errors.append(f"{where}: set default to {{{{customer_id}}}} from call variables")

    edges: dict[str, list[tuple[str, bool]]] = {}
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
                edges[name].append((target, _condition_has_verification(transition.get("condition"))))
        for field in ("passthrough_default_transition", "max_turn_transition"):
            target = node.get(field)
            if target is not None:
                if not isinstance(target, str) or (target not in all_nodes and not (name in globals_ and target == "__previous__")):
                    errors.append(f"{loc}.{field} {target!r} does not name a node")
                elif target != "__previous__" and (field != "passthrough_default_transition" or node.get("passthrough")):
                    edges[name].append((target, False))
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
            queue.extend(target for target, _ in edges.get(current, []))
        for name in nodes:
            if name not in reachable:
                errors.append(f"workflow.nodes.{name}: unreachable from initial_node")
        end_nodes = {n for n in reachable if isinstance(all_nodes.get(n), dict) and all_nodes[n].get("node_type") == "end_call"}
        if not end_nodes:
            errors.append("workflow: no reachable end_call node; add a terminal path")
        reverse = {n: [] for n in all_nodes}
        for source, links in edges.items():
            for target, _ in links:
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
            for target, gate in edges.get(name, []):
                queue.append((target, verified or gate))

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
            for target, _ in edges.get(name, []):
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
