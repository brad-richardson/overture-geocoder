from html.parser import HTMLParser
from pathlib import Path


PAGE = Path(__file__).parent.parent / "docs" / "index.html"


class PageStructure(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = []
        self.tabs = {}
        self.panels = {}

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        element_id = attributes.get("id")
        if element_id:
            self.ids.append(element_id)
        if attributes.get("role") == "tab":
            self.tabs[element_id] = attributes
        if attributes.get("role") == "tabpanel":
            self.panels[element_id] = attributes


def _page():
    return PAGE.read_text()


def _structure():
    parser = PageStructure()
    parser.feed(_page())
    return parser


def test_api_tester_has_accessible_forward_reverse_and_id_tabs():
    page = _structure()
    assert len(page.ids) == len(set(page.ids))
    assert set(page.tabs) == {"forward-tab", "reverse-tab", "id-tab"}
    assert set(page.panels) == {"forward-panel", "reverse-panel", "id-panel"}

    for tab_id, attributes in page.tabs.items():
        panel_id = attributes["aria-controls"]
        assert panel_id in page.panels
        assert page.panels[panel_id]["aria-labelledby"] == tab_id

    assert page.tabs["forward-tab"]["tabindex"] == "0"
    assert page.tabs["reverse-tab"]["tabindex"] == "-1"
    assert page.tabs["id-tab"]["tabindex"] == "-1"


def test_api_tester_tabs_support_roving_keyboard_focus():
    text = _page()
    assert "tab.tabIndex = selected ? 0 : -1" in text
    assert "event.key === 'ArrowRight'" in text
    assert "event.key === 'ArrowLeft'" in text
    assert "event.key === 'Home'" in text
    assert "event.key === 'End'" in text
    assert "nextTab.focus()" in text


def test_api_tester_can_chain_geocoder_results_into_id_lookup():
    text = _page()
    assert "request.client.search(query" in text
    assert "request.client.reverse(lat, lon" in text
    assert "/id/${encodeURIComponent(gersId)}" in text
    assert "await request.requestFetch(url" in text
    assert text.count("Test ID API") == 2
    assert "lookupResultId(result?.gers_id)" in text
    assert "idInput.value = gersId" in text


def test_api_tester_cancels_superseded_requests_and_escapes_output():
    text = _page()
    assert "controller.abort()" in text
    assert "const signal = combineSignals(init.signal, controller.signal)" in text
    assert "AbortSignal.any(signals)" in text
    assert "if (!isCurrentRequest(request)) return" in text
    assert "escapeHtml(result.id)" in text
    assert "escapeHtml(value)" in text


def test_api_tester_uses_published_client_with_encoded_id_lookup_contract():
    text = _page()
    assert "@bradrichardson/overture-geocoder@0.2.2" in text
    assert "gersId.length >= 2 && gersId.length <= 64" in text
    assert "/id/${encodeURIComponent(gersId)}" in text
    assert "response.status === 404" in text
    assert "if (!response.ok)" in text


def test_id_lookup_has_a_bounded_timeout():
    text = _page()
    assert "const ID_REQUEST_TIMEOUT_MS = 30000" in text
    assert "const timeoutController = new AbortController()" in text
    assert "timeoutController.abort()" in text
    assert "signal: timeoutController.signal" in text
    assert "ID lookup timed out. Please try again." in text
    assert "clearTimeout(timeoutId)" in text
