from types import SimpleNamespace
from django.test import SimpleTestCase

from .free_access import expanded_patterns, link_command_body


class FreeAccessWWWTests(SimpleTestCase):
    def test_www_also_allows_apex_and_apex_subdomains(self):
        patterns = [p for p, _ in expanded_patterns("www.taptapnetwork.com", True)]
        self.assertEqual(patterns, ["www.taptapnetwork.com", "taptapnetwork.com", "*.taptapnetwork.com"])

    def test_www_without_subdomains_still_allows_redirect_apex(self):
        patterns = [p for p, _ in expanded_patterns("https://www.taptapnetwork.com/login", False)]
        self.assertEqual(patterns, ["www.taptapnetwork.com", "taptapnetwork.com"])

    def test_link_command_contains_both_hosts(self):
        rows = [{"pattern": p, "comment": c} for p, c in expanded_patterns("www.taptapnetwork.com", True)]
        body = link_command_body({"patterns": rows})
        self.assertIn('dst-host="www.taptapnetwork.com"', body)
        self.assertIn('dst-host="taptapnetwork.com"', body)
        self.assertIn('dst-host="*.taptapnetwork.com"', body)
        self.assertIn('comment~"^TapTap-free:"', body)
