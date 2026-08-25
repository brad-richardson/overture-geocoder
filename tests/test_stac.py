"""Tests for Overture STAC release discovery."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import stac


@pytest.mark.parametrize(
    ("href", "expected"),
    [
        ("./2026-08-19.0/catalog.json", "2026-08-19.0"),
        (
            "https://stac.overturemaps.org/2026-08-19.0/catalog.json",
            "2026-08-19.0",
        ),
        ("2026-08-19.0/catalog.json?download=1", "2026-08-19.0"),
        ("https://stac.overturemaps.org/releases/catalog.json", None),
    ],
)
def test_release_from_href_accepts_relative_and_absolute_links(href, expected):
    assert stac.release_from_href(href) == expected


@pytest.mark.parametrize(
    "href",
    [
        "./2026-08-19.0/catalog.json",
        "https://stac.overturemaps.org/2026-08-19.0/catalog.json",
    ],
)
def test_latest_release_accepts_both_stac_link_shapes(monkeypatch, href):
    monkeypatch.setattr(
        stac,
        "get_catalog",
        lambda _url: {"links": [{"rel": "child", "latest": True, "href": href}]},
    )

    assert stac.get_latest_release() == "2026-08-19.0"


def test_list_releases_accepts_mixed_link_shapes(monkeypatch):
    monkeypatch.setattr(
        stac,
        "get_catalog",
        lambda _url: {
            "links": [
                {
                    "rel": "child",
                    "href": "https://stac.overturemaps.org/2026-08-19.0/catalog.json",
                },
                {"rel": "child", "href": "./2026-07-22.0/catalog.json"},
                {"rel": "self", "href": stac.STAC_ROOT},
                {"rel": "child", "href": "./not-a-release/catalog.json"},
            ]
        },
    )

    assert stac.list_releases() == ["2026-08-19.0", "2026-07-22.0"]


def test_latest_release_rejects_an_unparseable_latest_link(monkeypatch):
    monkeypatch.setattr(
        stac,
        "get_catalog",
        lambda _url: {
            "links": [
                {
                    "rel": "child",
                    "latest": True,
                    "href": "https://stac.overturemaps.org/releases/catalog.json",
                }
            ]
        },
    )

    with pytest.raises(ValueError, match="Could not find latest release"):
        stac.get_latest_release()
