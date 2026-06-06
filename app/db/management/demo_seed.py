from app.api.views import result_dic, result_teach


QUESTION_OPTIONS = (
    "Nunca",
    "Casi nunca",
    "A veces",
    "Casi siempre",
    "Siempre",
)

DEMO_FACTORS_BY_PREFIX = {
    "A-SEC": result_dic["SEC"],
    "A-EMS": result_dic["EMS"],
    "A-ES": result_dic["ES"],
    "M-SEC": result_teach["DOC"],
    "D-EMS": result_teach["DOC"],
    "D-ES": result_teach["DOC"],
}


def get_question_ids_by_prefix():
    question_ids_by_prefix = {}
    question_labels_by_prefix = {}
    for prefix, factors in DEMO_FACTORS_BY_PREFIX.items():
        seen_ids = set()
        labels = {}
        for factor in factors:
            for question_config in factor["question"]:
                question_id = question_config["id"]
                if question_id not in seen_ids:
                    seen_ids.add(question_id)
                    labels[question_id] = factor["name"]
        question_ids_by_prefix[prefix] = sorted(seen_ids)
        question_labels_by_prefix[prefix] = labels
    return question_ids_by_prefix, question_labels_by_prefix


def get_survey_type(prefix):
    return 0 if prefix.startswith("A-") else 1
