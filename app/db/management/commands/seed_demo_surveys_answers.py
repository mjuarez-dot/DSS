import random

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from app.db.models import Answer, Group, Question, School, Survey


QUESTION_COUNT = 122
STUDENT_PREFIX_BY_EMSTYPE = {
    0: "A-SEC",
    1: "A-EMS",
    2: "A-ES",
}
TEACHER_PREFIX_BY_EMSTYPE = {
    0: "M-SEC",
    1: "D-EMS",
    2: "D-ES",
}
SURVEY_DEFINITIONS = [
    ("A-SEC", 0),
    ("A-EMS", 0),
    ("A-ES", 0),
    ("M-SEC", 1),
    ("D-EMS", 1),
    ("D-ES", 1),
]


def option_values(question):
    return [
        value
        for value in [
            question.option_a,
            question.option_b,
            question.option_c,
            question.option_d,
            question.option_e,
        ]
        if value
    ]


def allocate_counts(total, size, rng):
    if size == 1:
        return [total]
    weights = [rng.randint(1, 100) for _ in range(size)]
    weighted_sum = sum(weights)
    base = [(total * weight) // weighted_sum for weight in weights]
    remainder = total - sum(base)
    while remainder > 0:
        idx = rng.randrange(size)
        base[idx] += 1
        remainder -= 1
    rng.shuffle(base)
    return base


class Command(BaseCommand):
    help = (
        "Seed surveys, questions for missing teacher prefixes, "
        "and demo aggregated answers for groups and schools."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--seed",
            type=int,
            default=20260605,
            help="Random seed for reproducible demo answers.",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        rng = random.Random(options["seed"])

        schools = list(School.objects.select_related("subsystem", "muni").order_by("school_key"))
        groups = list(Group.objects.select_related("school").order_by("school__school_key", "shift"))

        if not schools:
            raise CommandError("No demo schools found. Run seed_demo_schools first.")
        if not groups:
            raise CommandError("No groups found. Run seed_demo_schools first.")

        surveys = {}
        for survey_name, survey_type in SURVEY_DEFINITIONS:
            survey, _ = Survey.objects.update_or_create(
                survey_name=survey_name,
                defaults={
                    "number_1": "1",
                    "number_2": str(QUESTION_COUNT),
                    "type": survey_type,
                },
            )
            surveys[survey_name] = survey

        existing_questions = {
            question.key: question
            for question in Question.objects.select_related("survey").all()
        }

        # Ensure the teacher surveys exist and point D-EMS questions to the D-EMS survey.
        d_ems_survey = surveys["D-EMS"]
        for number in range(1, QUESTION_COUNT + 1):
            key = f"D-EMS-{number}"
            question = existing_questions.get(key)
            if question and question.survey_id != d_ems_survey.id:
                question.survey = d_ems_survey
                question.save(update_fields=["survey"])

        # Clone D-EMS into the missing teacher prefixes for demo compatibility.
        cloned_questions = 0
        for target_prefix in ["M-SEC", "D-ES"]:
            target_survey = surveys[target_prefix]
            for number in range(1, QUESTION_COUNT + 1):
                source = existing_questions.get(f"D-EMS-{number}")
                if not source:
                    raise CommandError(
                        "Missing D-EMS base questions; cannot clone teacher question set."
                    )
                target_key = f"{target_prefix}-{number}"
                target, created = Question.objects.update_or_create(
                    key=target_key,
                    defaults={
                        "survey": target_survey,
                        "question": source.question,
                        "option_a": source.option_a,
                        "option_b": source.option_b,
                        "option_c": source.option_c,
                        "option_d": source.option_d,
                        "option_e": source.option_e,
                    },
                )
                existing_questions[target_key] = target
                cloned_questions += int(created)

        # Refresh all question objects after any cloning/repointing.
        question_map = {
            question.key: question
            for question in Question.objects.select_related("survey").all()
        }

        student_prefixes = set(STUDENT_PREFIX_BY_EMSTYPE.values())
        teacher_prefixes = set(TEACHER_PREFIX_BY_EMSTYPE.values())
        student_questions = {
            prefix: [
                question_map[f"{prefix}-{number}"]
                for number in range(1, QUESTION_COUNT + 1)
                if f"{prefix}-{number}" in question_map
            ]
            for prefix in student_prefixes
        }
        teacher_questions = {
            prefix: [
                question_map[f"{prefix}-{number}"]
                for number in range(1, QUESTION_COUNT + 1)
                if f"{prefix}-{number}" in question_map
            ]
            for prefix in teacher_prefixes
        }

        if not all(student_questions.values()):
            raise CommandError("Missing one or more student question sets.")
        if not all(teacher_questions.values()):
            raise CommandError("Missing one or more teacher question sets.")

        # Clear previous demo answers for the prefixes managed by this command.
        managed_question_ids = {
            question.id
            for questions in [*student_questions.values(), *teacher_questions.values()]
            for question in questions
        }
        Answer.objects.filter(question_id__in=managed_question_ids).delete()

        answers_to_create = []

        for group in groups:
            prefix = STUDENT_PREFIX_BY_EMSTYPE[group.school.emstype]
            respondent_count = rng.randint(20, min(30, max(20, group.student_num)))
            for question in student_questions[prefix]:
                options = option_values(question)
                counts = allocate_counts(respondent_count, len(options), rng)
                for option, frequency in zip(options, counts):
                    answers_to_create.append(
                        Answer(
                            school=group.school,
                            group=group,
                            question=question,
                            answer=option,
                            frequency=frequency,
                        )
                    )

        for school in schools:
            prefix = TEACHER_PREFIX_BY_EMSTYPE[school.emstype]
            respondent_count = rng.randint(8, min(12, max(8, school.teacher_num)))
            for question in teacher_questions[prefix]:
                options = option_values(question)
                counts = allocate_counts(respondent_count, len(options), rng)
                for option, frequency in zip(options, counts):
                    answers_to_create.append(
                        Answer(
                            school=school,
                            group=None,
                            question=question,
                            answer=option,
                            frequency=frequency,
                        )
                    )

        Answer.objects.bulk_create(answers_to_create, batch_size=1000)

        self.stdout.write(
            self.style.SUCCESS(
                (
                    f"Surveys verificados={len(SURVEY_DEFINITIONS)}. "
                    f"Preguntas docentes clonadas={cloned_questions}. "
                    f"Answers demo creados={len(answers_to_create)}."
                )
            )
        )
