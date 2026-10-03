"""Generate TypeScript interfaces from the contract JSON Schemas.

snake_case is preserved deliberately. A camelCase mapping layer would be one
more transformation to keep correct across three languages, and the same
reasoning that rules out a C++ code generator rules this out too.
"""

from __future__ import annotations

import json
from pathlib import Path

BANNER = (
    "// GENERATED FILE - do not edit by hand.\n"
    "// Regenerate with: uv run python contract/generate.py\n"
    "// Source: contract/schemas/*.schema.json\n\n"
)

# schema stem -> exported root interface name
ROOTS = {
    "telemetry": "Telemetry",
    "event": "SmartWattEvent",
    "command": "Command",
    "fingerprints": "Fingerprints",
}


def _ts_type(node: dict, name_hint: str, nested: dict[str, str]) -> str:
    """Render one schema node as a TypeScript type, collecting nested interfaces."""
    if "const" in node:
        return json.dumps(node["const"])

    if "enum" in node:
        return " | ".join(json.dumps(v) for v in node["enum"])

    if "$ref" in node:
        return node["$ref"].rsplit("/", 1)[-1].title().replace("_", "")

    node_type = node.get("type")

    if isinstance(node_type, list):
        parts = [
            _ts_type({**node, "type": t}, name_hint, nested)
            for t in node_type
        ]
        return " | ".join(dict.fromkeys(parts))

    if node_type == "null":
        return "null"
    if node_type in ("number", "integer"):
        return "number"
    if node_type == "string":
        return "string"
    if node_type == "boolean":
        return "boolean"
    if node_type == "array":
        element = _ts_type(node.get("items", {}), name_hint + "Item", nested)
        if " | " in element:
            element = f"({element})"
        return f"{element}[]"
    if node_type == "object":
        nested[name_hint] = _render_interface(name_hint, node, nested)
        return name_hint

    return "unknown"


def _render_interface(name: str, node: dict, nested: dict[str, str]) -> str:
    required = set(node.get("required", []))
    lines = [f"export interface {name} {{"]
    for prop, sub in node.get("properties", {}).items():
        rendered = _ts_type(sub, name + _pascal(prop), nested)
        optional = "" if prop in required else "?"
        lines.append(f"  {prop}{optional}: {rendered};")
    lines.append("}")
    return "\n".join(lines)


def _pascal(snake: str) -> str:
    return "".join(part.capitalize() for part in snake.split("_"))


def _resolve_defs(schema: dict) -> dict[str, str]:
    """Render $defs as standalone interfaces so $ref resolves to a real name."""
    out: dict[str, str] = {}
    for key, node in schema.get("$defs", {}).items():
        name = _pascal(key)
        nested: dict[str, str] = {}
        body = _render_interface(name, node, nested)
        out.update(nested)
        out[name] = body
    return out


def generate_typescript(schema_dir: Path) -> str:
    blocks: list[str] = []
    for stem, root_name in ROOTS.items():
        schema = json.loads((schema_dir / f"{stem}.schema.json").read_text())
        nested: dict[str, str] = {}
        defs = _resolve_defs(schema)
        root = _render_interface(root_name, schema, nested)
        for body in defs.values():
            blocks.append(body)
        for body in nested.values():
            blocks.append(body)
        blocks.append(root)
    return BANNER + "\n\n".join(blocks) + "\n"


def main() -> None:
    here = Path(__file__).resolve().parent
    out = here.parent / "dashboard" / "src" / "types" / "contract.ts"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(generate_typescript(here / "schemas"), encoding="utf-8")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
