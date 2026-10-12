"""The dashboard loads nothing from third parties and runs no inline script (#1)."""
from html.parser import HTMLParser


class Scan(HTMLParser):
    """Real <script>/<link> elements and on...= attributes -- not text that merely
    looks like a tag inside an attribute value, such as the embed code fields."""

    def __init__(self, html: str) -> None:
        super().__init__()
        self.sources, self.inline_scripts, self.handlers = [], 0, []
        self._in_inline_script = False
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.handlers += [name for name in attrs if name.startswith("on")]
        if tag == "script":
            if attrs.get("src"):
                self.sources.append(attrs["src"])
            else:
                self._in_inline_script = True
        if tag == "link" and attrs.get("href"):
            self.sources.append(attrs["href"])

    def handle_data(self, data):
        if self._in_inline_script and data.strip():
            self.inline_scripts += 1

    def handle_endtag(self, tag):
        if tag == "script":
            self._in_inline_script = False


def _pages(client):
    pages = {path: client.get(path) for path in ("/", "/api", "/password")}
    client.get("/logout")
    pages["/login"] = client.get("/login")
    return pages


def test_no_third_party_script_or_stylesheet(admin_client, make_ad):
    make_ad(admin_client)
    for path, r in _pages(admin_client).items():
        assert r.status_code == 200, path
        scan = Scan(r.text)
        assert "/static/tailwind.css" in scan.sources, path
        assert "/static/dashboard.js" in scan.sources, path
        for src in scan.sources:
            assert src.startswith(("/static/", "data:")), f"{path} loads {src}"


def test_no_inline_script_on_dashboard_pages(admin_client, make_ad):
    make_ad(admin_client)
    for path, r in _pages(admin_client).items():
        scan = Scan(r.text)
        assert scan.handlers == [], f"{path} has inline event handlers {scan.handlers}"
        assert scan.inline_scripts == 0, f"{path} has inline script"


def test_dashboard_pages_send_a_strict_csp(admin_client, make_ad):
    slug = make_ad(admin_client)
    pages = _pages(admin_client)
    for path, r in pages.items():
        csp = r.headers.get("content-security-policy", "")
        assert "script-src 'self';" in csp, path
        assert "frame-ancestors 'none'" in csp, path
    # The embeddable frame must stay embeddable and keep its beacon script.
    frame = admin_client.get(f"/frame/{slug}")
    assert frame.status_code == 200
    assert "content-security-policy" not in frame.headers
    # Media and scripts for foreign pages are not HTML and get no dashboard policy.
    assert "content-security-policy" not in admin_client.get(f"/embed/{slug}.js").headers


def test_the_built_stylesheet_is_served_and_covers_the_templates(client):
    r = client.get("/static/tailwind.css")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/css")
    css = r.text
    # A few classes from the templates, including arbitrary values and variants.
    for selector in (r".grid-cols-2", r".md\:grid-cols-\[190px_1fr\]", r".border-\[3px\]",
                     r".file\:mr-2", r".sm\:p-6", r".max-w-6xl"):
        assert selector in css, selector
    assert client.get("/static/dashboard.js").headers["content-type"].startswith(
        ("text/javascript", "application/javascript"))
