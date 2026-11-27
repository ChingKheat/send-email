import unittest
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from app import ROOT, STATIC_FILES


class PageAssets(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "link" and attrs.get("rel") == "stylesheet":
            self.urls.append(attrs["href"])
        if tag == "script" and "src" in attrs:
            self.urls.append(attrs["src"])


class PageAssetTests(unittest.TestCase):
    def test_stylesheet_and_script_resolve_from_disk_and_local_server(self):
        page = ROOT / "web" / "index.html"
        assets = PageAssets()
        assets.feed(page.read_text(encoding="utf-8"))

        self.assertEqual(len(assets.urls), 2)
        for url in assets.urls:
            with self.subTest(url=url):
                local_url = urljoin(page.as_uri(), url)
                path = urlsplit(urljoin("http://127.0.0.1:8765/", url)).path
                self.assertEqual(local_url, STATIC_FILES[path][0].as_uri())
