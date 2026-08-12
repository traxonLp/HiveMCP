from __future__ import annotations

import io

import pytest
from openpyxl import load_workbook

from hivemcp.core.models import (
    Column,
    ConditionalFormat,
    RenderOptions,
    Sheet,
    SheetChart,
    SheetSpec,
)
from hivemcp.core.render.xlsx import render_spreadsheet


def reopen(data: bytes):
    return load_workbook(io.BytesIO(data))


def test_headers_types_and_formats(workbook: SheetSpec, options: RenderOptions) -> None:
    result = render_spreadsheet(workbook, options)
    assert result.sheet_names == ["Regionen"]

    sheet = reopen(result.data)["Regionen"]
    assert [cell.value for cell in sheet[1]] == ["Region", "Umsatz"]
    assert sheet["A1"].font.bold is True
    assert sheet["B2"].value == 4200000
    assert "€" in sheet["B2"].number_format
    assert sheet.freeze_panes == "A2"
    assert sheet.auto_filter.ref == "A1:B3"


def test_default_sheet_is_dropped(workbook: SheetSpec, options: RenderOptions) -> None:
    """openpyxl's Workbook() starts with a 'Sheet'; it must not survive into the output."""
    assert reopen(render_spreadsheet(workbook, options).data).sheetnames == ["Regionen"]


@pytest.mark.parametrize("payload", ["=1+1", "+SUM(A1)", "-2+3", "@cmd", "\tx"])
def test_text_that_looks_like_a_formula_is_neutralised(
    options: RenderOptions, payload: str
) -> None:
    """Untrusted text must never be evaluated by Excel (formula injection)."""
    spec = SheetSpec(
        title="T",
        sheets=[
            Sheet(name="S", columns=[Column(header="Notiz", key="n")], rows=[{"n": payload}])
        ],
    )
    value = reopen(render_spreadsheet(spec, options).data)["S"]["A2"].value
    assert value == f"'{payload}"


def test_explicit_formula_column_is_normalised(options: RenderOptions) -> None:
    spec = SheetSpec(
        title="T",
        sheets=[
            Sheet(
                name="S",
                columns=[Column(header="A", key="a", type="integer"),
                         Column(header="S", key="s", type="formula")],
                rows=[{"a": 1, "s": "A2*2"}, {"a": 2, "s": "=A3*2"}],
            )
        ],
    )
    sheet = reopen(render_spreadsheet(spec, options).data)["S"]
    assert sheet["B2"].value == "=A2*2"  # bare expression gets its '=' added
    assert sheet["B3"].value == "=A3*2"  # already-prefixed one is left alone


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("ja", True), ("nein", False), ("true", True), ("0", False), (True, True)],
)
def test_bool_coercion(options: RenderOptions, raw: object, expected: bool) -> None:
    spec = SheetSpec(
        title="T",
        sheets=[
            Sheet(
                name="S",
                columns=[Column(header="Aktiv", key="a", type="bool")],
                rows=[{"a": raw}],
            )
        ],
    )
    assert reopen(render_spreadsheet(spec, options).data)["S"]["A2"].value is expected


def test_non_numeric_value_in_numeric_column_does_not_crash(options: RenderOptions) -> None:
    spec = SheetSpec(
        title="T",
        sheets=[
            Sheet(
                name="S",
                columns=[Column(header="N", key="n", type="number")],
                rows=[{"n": "k.A."}],
            )
        ],
    )
    assert reopen(render_spreadsheet(spec, options).data)["S"]["A2"].value == "k.A."


def test_column_widths_explicit_and_estimated(options: RenderOptions) -> None:
    spec = SheetSpec(
        title="T",
        sheets=[
            Sheet(
                name="S",
                columns=[
                    Column(header="Fix", key="f", width=42.0),
                    Column(header="Auto", key="a"),
                ],
                rows=[{"f": "x", "a": "ein ziemlich langer Wert hier"}],
            )
        ],
    )
    dimensions = reopen(render_spreadsheet(spec, options).data)["S"].column_dimensions
    assert dimensions["A"].width == 42.0
    assert dimensions["B"].width == len("ein ziemlich langer Wert hier") + 2


def test_conditional_formats_and_charts(options: RenderOptions) -> None:
    spec = SheetSpec(
        title="T",
        sheets=[
            Sheet(
                name="S",
                columns=[Column(header="K", key="k"), Column(header="V", key="v", type="number")],
                rows=[{"k": "a", "v": 1}, {"k": "b", "v": 2}],
                conditional_formats=[
                    ConditionalFormat(range="B2:B3", rule="data_bar"),
                    ConditionalFormat(range="B2:B3", rule="duplicates"),
                ],
                charts=[
                    SheetChart(
                        chart_type="column",
                        categories_range="A2:A3",
                        values_range="B1:B3",
                        title="Werte",
                    )
                ],
            )
        ],
    )
    sheet = reopen(render_spreadsheet(spec, options).data)["S"]

    rule_types = {
        rule.type
        for rules in sheet.conditional_formatting._cf_rules.values()
        for rule in rules
    }
    assert {"dataBar", "duplicateValues"} <= rule_types
    assert len(sheet._charts) == 1


def test_sheet_name_validation_rejects_excel_illegal_characters() -> None:
    with pytest.raises(ValueError, match="sheet name"):
        Sheet(name="a/b", columns=[Column(header="H", key="h")])


def test_duplicate_sheet_names_are_rejected() -> None:
    columns = [Column(header="H", key="h")]
    with pytest.raises(ValueError, match="unique"):
        SheetSpec(
            title="T",
            sheets=[Sheet(name="S", columns=columns), Sheet(name="s", columns=columns)],
        )
