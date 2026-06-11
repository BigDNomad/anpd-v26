"""Tests for V25 principles_loader."""
import json
import pytest
from principles_loader import load_principles, filter_principles, format_principles_for_prompt, load_principles_for_component


@pytest.fixture
def principles_file(tmp_path):
    data = {
        "principles": [
            {
                "id": "TEST-GENERIC",
                "name": "Test Generic Principle",
                "scope": "GENERIC",
                "severity": "CLASS_B",
                "description": "A generic test principle.",
                "components_inject_into_prompt": ["synopsis_generator", "scene_writer"],
                "exceptions": []
            },
            {
                "id": "TEST-WAR",
                "name": "Test War Principle",
                "scope": "WAR-FICTION",
                "severity": "CLASS_A",
                "description": "A war-fiction test principle.",
                "components_inject_into_prompt": ["synopsis_generator"],
                "exceptions": ["When explicitly overridden"]
            },
            {
                "id": "TEST-OTHER",
                "name": "Test Other Component",
                "scope": "GENERIC",
                "severity": "CLASS_B",
                "description": "Only for scene_writer.",
                "components_inject_into_prompt": ["scene_writer"],
                "exceptions": []
            }
        ]
    }
    path = tmp_path / "principles.json"
    path.write_text(json.dumps(data))
    return str(path)


def test_load_principles(principles_file):
    principles = load_principles(principles_file)
    assert len(principles) == 3


def test_filter_by_component(principles_file):
    principles = load_principles(principles_file)
    filtered = filter_principles(principles, "synopsis_generator")
    assert len(filtered) == 2
    ids = [p["id"] for p in filtered]
    assert "TEST-GENERIC" in ids
    assert "TEST-WAR" in ids
    assert "TEST-OTHER" not in ids


def test_filter_by_scope(principles_file):
    principles = load_principles(principles_file)
    filtered = filter_principles(principles, "synopsis_generator", scope_filter=["GENERIC"])
    assert len(filtered) == 1
    assert filtered[0]["id"] == "TEST-GENERIC"


def test_format_for_prompt(principles_file):
    principles = load_principles(principles_file)
    filtered = filter_principles(principles, "synopsis_generator")
    text = format_principles_for_prompt(filtered)
    assert "<craft_principles>" in text
    assert "TEST-GENERIC" in text
    assert "TEST-WAR" in text
    assert "CLASS_A" in text


def test_load_for_component(principles_file):
    text = load_principles_for_component("synopsis_generator", principles_path=principles_file)
    assert "TEST-GENERIC" in text
    assert "TEST-WAR" in text


def test_load_nonexistent_file():
    with pytest.raises(FileNotFoundError):
        load_principles("/nonexistent/principles.json")


def test_load_real_principles():
    """Test loading the actual craft_principles.json if available."""
    path = "/anpd/v25/principles/craft_principles.json"
    if not __import__("os").path.exists(path):
        pytest.skip("Craft principles not staged")
    principles = load_principles(path)
    assert len(principles) >= 10
    ids = [p["id"] for p in principles]
    assert "OPERATOR-STRUCTURE-FIDELITY" in ids
    assert "NO-PRISONERS-DOCTRINE" in ids
