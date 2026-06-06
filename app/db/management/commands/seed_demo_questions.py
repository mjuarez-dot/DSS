from django.core.management.base import BaseCommand

from app.db.management.demo_seed import (
    DEMO_FACTORS_BY_PREFIX,
    QUESTION_OPTIONS,
    get_question_ids_by_prefix,
    get_survey_type,
)
from app.db.models import Question, Survey


class Command(BaseCommand):
    help = "Seed the demo survey/question bank used by the rest of the demo data."

    def handle(self, *args, **options):
        seed_demo_questions(self)


def seed_demo_questions(command=None):
    question_ids_by_prefix, question_labels_by_prefix = get_question_ids_by_prefix()

    surveys_created = 0
    surveys_updated = 0
    questions_created = 0
    questions_updated = 0

    for prefix, factors in DEMO_FACTORS_BY_PREFIX.items():
        question_ids = question_ids_by_prefix[prefix]
        question_labels = question_labels_by_prefix[prefix]
        survey, survey_created = Survey.objects.update_or_create(
            survey_name=prefix,
            defaults={
                "number_1": "1",
                "number_2": str(max(question_ids) if question_ids else 1),
                "type": get_survey_type(prefix),
            },
        )
        if survey_created:
            surveys_created += 1
        else:
            surveys_updated += 1

        for question_id in question_ids:
            question_key = f"{prefix}-{question_id}"
            _, question_created = Question.objects.update_or_create(
                key=question_key,
                defaults={
                    "survey": survey,
                    "question": f"Pregunta demo {question_labels[question_id]} ({question_key})",
                    "option_a": QUESTION_OPTIONS[0],
                    "option_b": QUESTION_OPTIONS[1],
                    "option_c": QUESTION_OPTIONS[2],
                    "option_d": QUESTION_OPTIONS[3],
                    "option_e": QUESTION_OPTIONS[4],
                },
            )
            if question_created:
                questions_created += 1
            else:
                questions_updated += 1

    if command is None:
        return {
            "surveys_created": surveys_created,
            "surveys_updated": surveys_updated,
            "questions_created": questions_created,
            "questions_updated": questions_updated,
        }

    command.stdout.write(
        command.style.SUCCESS(
            (
                f"Surveys creados={surveys_created}, actualizados={surveys_updated}. "
                f"Preguntas creadas={questions_created}, actualizadas={questions_updated}."
            )
        )
    )
