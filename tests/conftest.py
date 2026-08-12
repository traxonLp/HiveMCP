from __future__ import annotations

import base64

import pytest

from hivemcp.config import Settings
from hivemcp.core.models import (
    Bullet,
    ChartData,
    ChartSeries,
    Column,
    DeckSpec,
    DocSpec,
    Heading,
    ImageRef,
    Paragraph,
    RenderOptions,
    Sheet,
    SheetSpec,
    Slide,
    TableData,
)

# Smallest valid PNG: 1x1 transparent pixel.
TINY_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQ"
    "AAAABJRU5ErkJggg=="
)


@pytest.fixture
def tiny_png() -> str:
    base64.b64decode(TINY_PNG_B64, validate=True)  # fail loudly if the constant rots
    return TINY_PNG_B64


@pytest.fixture
def options() -> RenderOptions:
    return RenderOptions()


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        environment="dev",
        data_dir=tmp_path,
        auth_token="test-token",
        signing_key="test-signing-key",
        public_url="http://testserver",
    )


@pytest.fixture
def deck(tiny_png: str) -> DeckSpec:
    return DeckSpec(
        title="Quartalsbericht",
        slides=[
            Slide(layout="title", title="Q3 2026", subtitle="Plattform"),
            Slide(layout="section", title="Ergebnisse"),
            Slide(
                layout="title_content",
                title="Kernaussagen",
                bullets=[
                    Bullet(
                        text="Umsatz gestiegen",
                        children=[Bullet(text="DACH +12%", children=[Bullet(text="trotz FX")])],
                    ),
                    Bullet(text="Kosten stabil", bold=True),
                ],
                notes="Langsam sprechen.",
            ),
            Slide(
                layout="two_content",
                title="Vorher / Nachher",
                bullets=[Bullet(text="Manuell")],
                bullets_right=[Bullet(text="Automatisiert")],
            ),
            Slide(
                layout="table",
                title="Regionen",
                table=TableData(headers=["Region", "Umsatz"], rows=[["DACH", "4.2M"]]),
            ),
            Slide(
                layout="chart",
                title="Verlauf",
                chart=ChartData(
                    chart_type="column",
                    categories=["Q1", "Q2"],
                    series=[ChartSeries(name="2026", values=[1.0, 2.0])],
                ),
            ),
            Slide(
                layout="image",
                title="Architektur",
                image=ImageRef(data_base64=TINY_PNG_B64, width_cm=6, alt_text="Diagramm"),
            ),
            Slide(layout="blank", body="Danke."),
        ],
    )


@pytest.fixture
def document() -> DocSpec:
    return DocSpec(
        title="Betriebshandbuch",
        subtitle="Version 0.1",
        author="Plattform-Team",
        blocks=[
            Heading(text="Einleitung", level=1),
            Paragraph(text="Dieses Dokument beschreibt den Betrieb.", alignment="justify"),
        ],
    )


@pytest.fixture
def workbook() -> SheetSpec:
    return SheetSpec(
        title="Umsatz",
        sheets=[
            Sheet(
                name="Regionen",
                columns=[
                    Column(header="Region", key="region"),
                    Column(header="Umsatz", key="umsatz", type="currency"),
                ],
                rows=[
                    {"region": "DACH", "umsatz": 4200000},
                    {"region": "UK", "umsatz": 1100000},
                ],
            )
        ],
    )
