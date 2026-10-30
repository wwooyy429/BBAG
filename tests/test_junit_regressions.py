"""S0-002: synthetic JUnit controls, never actual developer evidence."""

from pathlib import Path

import pytest

from bbag.junit import validate_junit


def validate_xml(tmp_path: Path, xml: str) -> dict[str, int]:
    path = tmp_path / "synthetic-junit.xml"
    path.write_text(xml, encoding="utf-8")
    return validate_junit(path)


def suite(cases: str, *, tests: str = "1", failures: str = "0", errors: str = "0", skipped: str = "0") -> str:
    return (f'<testsuite tests="{tests}" failures="{failures}" errors="{errors}" skipped="{skipped}">'
            f"{cases}</testsuite>")


@pytest.mark.parametrize("details", ["", '<testcase><failure/></testcase>', '<testcase><error/></testcase>',
                                     '<testcase><skipped/></testcase>'])
def test_reject_claimed_success_without_successful_case(tmp_path: Path, details: str) -> None:
    with pytest.raises(ValueError):
        validate_xml(tmp_path, f"<testsuites>{suite(details)}</testsuites>")


@pytest.mark.parametrize("attribute", ["tests", "failures", "errors", "skipped"])
@pytest.mark.parametrize("value", [None, "-1", "true", "1.0", "+1", "", " 1 ", "１"])
def test_reject_missing_or_noninteger_summary(tmp_path: Path, attribute: str, value: str | None) -> None:
    xml = suite("<testcase/>")
    original = f'{attribute}="{1 if attribute == "tests" else 0}"'
    replacement = "" if value is None else f'{attribute}="{value}"'
    with pytest.raises(ValueError):
        validate_xml(tmp_path, xml.replace(original, replacement))


@pytest.mark.parametrize("details,counts", [
    ("", {"tests": "0"}),
    ("<testcase><skipped/></testcase>", {"skipped": "1"}),
    ("<testcase><failure/></testcase>", {"failures": "1"}),
    ("<testcase><error/></testcase>", {"errors": "1"}),
])
def test_reject_consistent_empty_skipped_failed_or_errored_suite(
    tmp_path: Path, details: str, counts: dict[str, str]
) -> None:
    with pytest.raises(ValueError, match="successful nonempty"):
        validate_xml(tmp_path, suite(details, **counts))


def test_accept_real_pytest_shape_and_count_partial_skips(tmp_path: Path) -> None:
    cases = ('<testcase classname="test_sample" name="pass" time="0.01"><properties>'
             '<property name="fixture" value="synthetic"/></properties><system-out>ok</system-out></testcase>'
             '<testcase classname="test_sample" name="skip"><skipped type="pytest.skip">reason</skipped>'
             '</testcase>')
    assert validate_xml(tmp_path, f"<testsuites>{suite(cases, tests='2', skipped='1')}</testsuites>") == {
        "tests": 2, "failures": 0, "errors": 0, "skipped": 1,
    }


@pytest.mark.parametrize("attributes", [
    "", 'status="run"', 'result="completed"', 'status="run" result="completed"',
])
def test_accept_absent_or_explicit_completed_execution_attributes(tmp_path: Path, attributes: str) -> None:
    assert validate_xml(tmp_path, suite(f"<testcase {attributes}/>"))["tests"] == 1


@pytest.mark.parametrize("attributes", [
    'status="notrun" result="suppressed"', 'status="unknown"',
    'result="suppressed"', 'status="run" result="unknown"',
])
def test_reject_nonexecuted_or_unknown_execution_attributes(tmp_path: Path, attributes: str) -> None:
    with pytest.raises(ValueError, match="unsupported execution"):
        validate_xml(tmp_path, suite(f"<testcase {attributes}/>"))


def test_nested_suites_validate_aggregates_without_double_counting(tmp_path: Path) -> None:
    nested = ("<testsuites>" + suite("<testcase/>")
              + '<testsuites tests="1" failures="0" errors="0" skipped="0">'
              + suite("<testcase/>") + "</testsuites></testsuites>")
    xml = suite("<testcase/>" + nested, tests="3")
    assert validate_xml(tmp_path, xml) == {"tests": 3, "failures": 0, "errors": 0, "skipped": 0}


def test_nested_inconsistent_child_cannot_be_hidden_by_correct_parent(tmp_path: Path) -> None:
    child = suite("<testcase/>", tests="2")
    with pytest.raises(ValueError, match="summary does not match"):
        validate_xml(tmp_path, suite(child))


@pytest.mark.parametrize("root", [
    '<testsuites tests="2" failures="0" errors="0" skipped="0">{suite}</testsuites>',
    '<testsuites tests="1">{suite}</testsuites>',
])
def test_root_container_summary_must_be_complete_and_consistent(tmp_path: Path, root: str) -> None:
    with pytest.raises(ValueError):
        validate_xml(tmp_path, root.format(suite=suite("<testcase/>")))


@pytest.mark.parametrize("xml", [
    "not XML", "<unsupported/>", '<testsuites xmlns="urn:unsupported"/>',
    '<testsuites><testcase/></testsuites>',
    suite('<unexpected><testcase/></unexpected>'),
    suite('<testcase><failure/><skipped/></testcase>'),
    suite('<testcase><unexpected/></testcase>'),
    suite('<testcase><system-out><failure/></system-out></testcase>'),
    suite('<testcase><skipped><testcase/></skipped></testcase>'),
    suite('<testcase/><properties><testcase/></properties>'),
])
def test_reject_unsupported_or_ambiguous_structure(tmp_path: Path, xml: str) -> None:
    with pytest.raises(ValueError):
        validate_xml(tmp_path, xml)


def test_reject_missing_junit_file(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="missing"):
        validate_junit(tmp_path / "missing.xml")
