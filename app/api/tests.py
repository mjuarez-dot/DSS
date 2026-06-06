from django.test import SimpleTestCase
from django.urls import resolve, reverse

from app.api.views import (
    IndexView,
    LoginView,
    MainView,
    ReportView,
    SurveyFormView,
)


class UrlSmokeTests(SimpleTestCase):
    def test_core_named_urls_reverse(self):
        self.assertEqual(reverse("index"), "/")
        self.assertEqual(reverse("login"), "/login")
        self.assertEqual(reverse("main"), "/main")
        self.assertEqual(reverse("survey_form"), "/survey_form")
        self.assertEqual(reverse("report"), "/report")

    def test_core_views_resolve(self):
        self.assertIs(resolve("/").func.view_class, IndexView)
        self.assertIs(resolve("/login").func.view_class, LoginView)
        self.assertIs(resolve("/main").func.view_class, MainView)
        self.assertIs(resolve("/survey_form").func.view_class, SurveyFormView)
        self.assertIs(resolve("/report").func.view_class, ReportView)
