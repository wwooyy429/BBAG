"""Validate S0 JUnit evidence against actual testcase details.

Supported XML is an unnamespaced ``testsuite`` or ``testsuites`` tree. Every
suite declares all four counts for its complete subtree. A ``testsuites``
container may omit counts; if it declares any, it must declare all four.
Counts are computed from testcase nodes, so nested suite summaries never add
the same test twice. Optional testcase execution attributes are supported only
as ``status="run"`` and ``result="completed"``; nonexecution and unknown dialect
values are rejected. This checks evidence consistency, not its authenticity.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

_COUNTS = ("tests", "failures", "errors", "skipped")
_OUTCOMES = {"failure": "failures", "error": "errors", "skipped": "skipped"}
_METADATA = {"properties", "system-out", "system-err"}
_STRUCTURAL = {"testsuite", "testsuites", "testcase", *_OUTCOMES}


def _empty_counts() -> dict[str, int]:
    return dict.fromkeys(_COUNTS, 0)


def _check_metadata(element: ET.Element) -> None:
    # Outcome or testcase nodes hidden in metadata are not valid JUnit details.
    if any(node.tag in _STRUCTURAL for node in element.iter()):
        raise ValueError("JUnit evidence contains test details in metadata")


def _case_counts(case: ET.Element) -> dict[str, int]:
    if case.get("status") not in {None, "run"} or case.get("result") not in {None, "completed"}:
        raise ValueError("JUnit testcase contains an unsupported execution status or result")
    counts = _empty_counts()
    counts["tests"] = 1
    outcome: str | None = None
    for child in case:
        if child.tag in _OUTCOMES:
            if outcome is not None:
                raise ValueError("JUnit testcase has multiple outcome details")
            outcome = child.tag
            if any(node.tag in _STRUCTURAL for node in child.iter() if node is not child):
                raise ValueError("JUnit outcome contains nested test details")
            counts[_OUTCOMES[outcome]] = 1
        elif child.tag in _METADATA:
            _check_metadata(child)
        else:
            raise ValueError("JUnit testcase contains an unsupported child node")
    return counts


def _check_summary(element: ET.Element, actual: dict[str, int]) -> None:
    required = element.tag == "testsuite" or any(key in element.attrib for key in _COUNTS)
    if not required:
        return
    for key in _COUNTS:
        value = element.get(key)
        if value is None or re.fullmatch(r"[0-9]+", value) is None:
            raise ValueError(f"JUnit {element.tag} must declare a nonnegative integer {key} count")
        if int(value) != actual[key]:
            raise ValueError(f"JUnit {element.tag} {key} summary does not match testcase details")


def _subtree_counts(element: ET.Element) -> dict[str, int]:
    if element.tag not in {"testsuite", "testsuites"}:
        raise ValueError("JUnit root must be an unnamespaced testsuite or testsuites")
    counts = _empty_counts()
    for child in element:
        if child.tag in {"testsuite", "testsuites"}:
            child_counts = _subtree_counts(child)
        elif child.tag == "testcase" and element.tag == "testsuite":
            child_counts = _case_counts(child)
        elif child.tag in _METADATA and element.tag == "testsuite":
            _check_metadata(child)
            continue
        else:
            raise ValueError(f"JUnit {element.tag} contains an unsupported child node")
        for key in _COUNTS:
            counts[key] += child_counts[key]
    _check_summary(element, counts)
    return counts


def validate_junit(path: Path) -> dict[str, int]:
    """Require consistent JUnit details with at least one successful test.

    Failed or errored cases always block packaging. Skipped cases may coexist
    with successful cases, but cannot stand in for executed tests.
    """
    if not path.is_file():
        raise ValueError("Required JUnit evidence is missing")
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        raise ValueError("JUnit evidence is not valid XML") from exc
    counts = _subtree_counts(root)
    if (counts["tests"] <= counts["skipped"] or counts["failures"] or counts["errors"]):
        raise ValueError("JUnit evidence does not show a successful nonempty executed test suite")
    return counts
