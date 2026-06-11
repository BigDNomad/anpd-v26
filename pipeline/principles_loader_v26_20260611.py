"""
principles_loader.py — V25 Craft Principles Loader
ANPD V25 | Version: 20260509

Loads craft_principles.json and formats selected principles as prompt-injection
sections for generator system prompts.
"""

import json
import os

DEFAULT_PRINCIPLES_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "principles", "craft_principles.json"
)


def load_principles(principles_path: str = None) -> list:
    """Load craft principles from JSON file.

    Returns list of principle dicts.
    """
    path = principles_path or DEFAULT_PRINCIPLES_PATH
    if not os.path.exists(path):
        raise FileNotFoundError(f"Craft principles file not found: {path}")

    with open(path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    if isinstance(data, dict) and "principles" in data:
        return data["principles"]
    elif isinstance(data, list):
        return data
    else:
        raise ValueError(f"Unexpected craft_principles.json structure: expected dict with 'principles' key or list")


def filter_principles(principles: list, component: str, scope_filter: list = None) -> list:
    """Filter principles applicable to a component and scope.

    Args:
        principles: full list of principle dicts
        component: component name (e.g., "synopsis_generator")
        scope_filter: list of scope values to include (e.g., ["GENERIC", "WAR-FICTION"])

    Returns filtered list of principles.
    """
    filtered = []
    for p in principles:
        # Check component applicability
        components = p.get("components_inject_into_prompt", [])
        if component not in components and "*" not in components:
            continue

        # Check scope filter
        if scope_filter:
            p_scope = p.get("scope", "GENERIC")
            if p_scope not in scope_filter:
                continue

        filtered.append(p)

    return filtered


def format_principles_for_prompt(principles: list) -> str:
    """Format principles list as prompt-injection text.

    Returns markdown text suitable for inclusion in LLM prompts.
    """
    if not principles:
        return ""

    sections = []
    sections.append("<craft_principles>")
    sections.append("The following craft principles MUST be followed in all generated content.")
    sections.append("Violations are flagged by the comparator and require regeneration.")
    sections.append("")

    for p in principles:
        pid = p.get("id", "UNKNOWN")
        name = p.get("name", "Unnamed")
        severity = p.get("severity", "CLASS_B")
        description = p.get("description", "")
        exceptions = p.get("exceptions", [])

        section = f"### {pid}: {name} [{severity}]\n{description}"

        if exceptions:
            section += "\nExceptions: " + "; ".join(exceptions)

        sections.append(section)
        sections.append("")

    sections.append("</craft_principles>")
    return "\n".join(sections)


def load_principles_for_component(
    component: str,
    scope_filter: list = None,
    principles_path: str = None,
) -> str:
    """Load and format principles for a specific component.

    Returns formatted prompt-injection text containing the principles
    applicable to the named component, filtered by scope.
    """
    principles = load_principles(principles_path)
    filtered = filter_principles(principles, component, scope_filter)
    return format_principles_for_prompt(filtered)


if __name__ == "__main__":
    import sys
    component = sys.argv[1] if len(sys.argv) > 1 else "synopsis_generator"
    path = sys.argv[2] if len(sys.argv) > 2 else None
    text = load_principles_for_component(component, scope_filter=["GENERIC", "WAR-FICTION"], principles_path=path)
    print(text)
