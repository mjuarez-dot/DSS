from django.test import SimpleTestCase

from app.db.models import (
    Answer,
    Group,
    Municipality,
    Question,
    ReportResult,
    School,
    State,
    Subsystem,
    Survey,
    UserApp,
)


class ModelSmokeTests(SimpleTestCase):
    def test_expected_model_classes_are_available(self):
        models = {
            UserApp,
            State,
            Municipality,
            Subsystem,
            School,
            Group,
            Survey,
            Question,
            Answer,
            ReportResult,
        }
        self.assertEqual(len(models), 10)
