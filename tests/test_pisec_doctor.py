import json
import unittest

from scripts.pisec.doctor import _collie_route_ok, _funnel_is_disabled


class DoctorCollieTests(unittest.TestCase):
    def test_collie_route_requires_https_loopback_root_and_public_host(self):
        route = {
            "TCP": {"443": {"HTTPS": True}},
            "Web": {
                "pisec.example.ts.net:443": {
                    "Handlers": {"/": {"Proxy": "http://127.0.0.1:8787", "Extra": "ignored"}}
                }
            },
        }
        self.assertTrue(_collie_route_ok(json.dumps(route), "pisec.example.ts.net"))
        self.assertTrue(_collie_route_ok(json.dumps({key: value for key, value in route.items() if key != "TCP"}), "pisec.example.ts.net"))
        extra_route = json.loads(json.dumps(route))
        extra_route["Web"]["other.example.ts.net:443"] = route["Web"]["pisec.example.ts.net:443"]
        self.assertFalse(_collie_route_ok(json.dumps(extra_route), "pisec.example.ts.net"))
        route["TCP"]["443"]["HTTPS"] = False
        self.assertFalse(_collie_route_ok(json.dumps(route), "pisec.example.ts.net"))
        route["TCP"]["443"]["HTTPS"] = True
        route["Web"]["pisec.example.ts.net:8443"] = route["Web"].pop("pisec.example.ts.net:443")
        self.assertFalse(_collie_route_ok(json.dumps(route), "pisec.example.ts.net"))
        route["Web"]["pisec.example.ts.net:443"] = route["Web"].pop("pisec.example.ts.net:8443")
        route["Web"]["pisec.example.ts.net:443"]["Handlers"]["/"]["Proxy"] = "http://0.0.0.0:8787"
        self.assertFalse(_collie_route_ok(json.dumps(route), "pisec.example.ts.net"))

    def test_funnel_parser_fails_closed_for_enabled_or_invalid_state(self):
        self.assertFalse(_funnel_is_disabled("not json"))
        self.assertTrue(_funnel_is_disabled('{"AllowFunnel":{"pisec.example.ts.net:443":false}}'))
        self.assertFalse(_funnel_is_disabled('{"AllowFunnel":{"pisec.example.ts.net:443":true}}'))
        self.assertFalse(_funnel_is_disabled('{"funnel":{"enabled":true}}'))
        self.assertTrue(_funnel_is_disabled('{"TCP":{},"Web":{}}'))
        self.assertFalse(_funnel_is_disabled("funnel enabled"))


if __name__ == "__main__":
    unittest.main()
