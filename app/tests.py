from django.test import SimpleTestCase
from django.urls import clear_url_caches


class DjangoConfigurationSmokeTests(SimpleTestCase):
    def test_settings_module_and_urls_load(self):
        clear_url_caches()
        __import__("settings")
        __import__("urls")
