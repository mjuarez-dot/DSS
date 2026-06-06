from urllib import request
from app.db.forms import *
from app.db.models import *
from .tasks import save_excel_data, pdf_reports_task

from django.contrib import messages
from django.contrib.auth import login, logout, authenticate
from django.contrib.auth.hashers import make_password
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Q
from django.http import (
    HttpResponse,
    HttpResponseRedirect,
    JsonResponse,
    HttpResponseNotFound,
    FileResponse,
)
from django.conf import settings
from django.shortcuts import render
from django.urls import reverse_lazy
from django.views.generic import View
import os, json

MAP_LEVEL_CONFIG = {
    "SEC": {
        "emstype": 0,
        "student_prefix": "A-SEC",
        "teacher_prefix": "M-SEC",
        "subsystem_prefix": "SEC-",
        "label": "Secundaria",
    },
    "EMS": {
        "emstype": 1,
        "student_prefix": "A-EMS",
        "teacher_prefix": "D-EMS",
        "subsystem_prefix": "EMS-",
        "label": "Media Superior",
    },
    "ES": {
        "emstype": 2,
        "student_prefix": "A-ES",
        "teacher_prefix": "D-ES",
        "subsystem_prefix": "ES-",
        "label": "Superior",
    },
}

MAP_FOCUS_FACTORS = ["Ansiedad", "Anhedonia"]
MAP_GEOJSON_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    "static",
    "assets",
    "json",
    "jalisco_municipios.json",
)


def _normalize_map_text(value):
    if not value:
        return ""
    value = value.lower()
    replacements = str.maketrans(
        {
            "á": "a",
            "é": "e",
            "í": "i",
            "ó": "o",
            "ú": "u",
            "ü": "u",
            "ñ": "n",
        }
    )
    normalized = value.translate(replacements)
    return " ".join("".join(ch if ch.isalnum() else " " for ch in normalized).split())


def _resolve_map_subsystem(level):
    prefix = MAP_LEVEL_CONFIG.get(level, MAP_LEVEL_CONFIG["SEC"])["subsystem_prefix"]
    return Subsystem.objects.filter(abrev__startswith=prefix).order_by("name")


def _get_map_questions(prefix, factors):
    question_map = {}
    for factor in factors:
        for question_config in factor["question"]:
            question_key = f"{prefix}-{question_config['id']}"
            if question_key not in question_map:
                question_map[question_key] = Question.objects.filter(key=question_key).last()
    return question_map


def _get_map_prevalence(factor, total_res):
    if total_res < factor["prevalence_min"][0]:
        return 0
    if total_res < factor["prevalence_min"][1]:
        return 1
    if total_res < factor["prevalence_mid"][1]:
        return 2
    if total_res < factor["prevalence_high"][1]:
        return 3
    if "prevalence_veryhigh" in factor and total_res <= factor["prevalence_veryhigh"][1]:
        return 4
    return 3


def _build_map_payload_for_schools(level, school_ids):
    if not school_ids:
        empty_payload = {"social_health": -1, "respondent_count": 0, "factors": []}
        return {
            "student": empty_payload.copy(),
            "teacher": empty_payload.copy(),
        }

    config = MAP_LEVEL_CONFIG.get(level, MAP_LEVEL_CONFIG["SEC"])
    payload = {}
    calculations = (
        ("student", result_dic[level], config["student_prefix"]),
        ("teacher", result_teach["DOC"], config["teacher_prefix"]),
    )

    for audience, factors, prefix in calculations:
        base_question = Question.objects.filter(key__startswith=prefix).first()
        if not base_question:
            payload[audience] = {"social_health": -1, "respondent_count": 0, "factors": []}
            continue

        respondent_count = sum(
            answer.frequency
            for answer in Answer.objects.filter(
                school_id__in=school_ids,
                question_id=base_question.id,
            )
        )
        if respondent_count <= 0:
            payload[audience] = {"social_health": -1, "respondent_count": 0, "factors": []}
            continue

        question_map = _get_map_questions(prefix, factors)
        factor_payload = []
        social_health_score = 0
        for factor in factors:
            percentages = []
            for question_config in factor["question"]:
                question_key = f"{prefix}-{question_config['id']}"
                question = question_map.get(question_key)
                if not question:
                    continue
                selected_answers = [
                    question.get_option(option_key)
                    for option_key in question_config["answer"]
                ]
                selected_answers = [answer for answer in selected_answers if answer]
                result_count = sum(
                    answer.frequency
                    for answer in Answer.objects.filter(
                        school_id__in=school_ids,
                        question_id=question.id,
                        answer__in=selected_answers,
                    )
                )
                percentages.append(round((result_count / respondent_count) * 100, 2))

            if not percentages:
                continue

            total_res = min(100, round(sum(percentages) / len(percentages), 2))
            prevalence = _get_map_prevalence(factor, total_res)
            if prevalence > 0:
                social_health_score += 1

            factor_payload.append(
                {
                    "factor": factor["name"],
                    "value": total_res,
                    "prevalence": prevalence,
                    "count": round(respondent_count * (total_res / 100)),
                }
            )

        payload[audience] = {
            "social_health": max(0, 1000 - (77 * social_health_score)),
            "respondent_count": respondent_count,
            "factors": factor_payload,
        }

    return payload


def _load_jalisco_map_geojson():
    with open(MAP_GEOJSON_PATH, "r", encoding="utf-8") as geojson_file:
        return json.load(geojson_file)


def _public_url(path):
    base_url = getattr(settings, "PUBLIC_BASE_URL", "").rstrip("/")
    if base_url:
        return f"{base_url}{path}"
    return path


def _build_jalisco_map_response(level, subsystem=None):
    config = MAP_LEVEL_CONFIG.get(level, MAP_LEVEL_CONFIG["SEC"])
    schools = School.objects.filter(
        emstype=config["emstype"],
        muni__state__name="Jalisco",
    ).select_related("muni", "subsystem")
    if subsystem:
        schools = schools.filter(subsystem=subsystem)

    school_ids_by_muni = {}
    for school in schools:
        school_ids_by_muni.setdefault(school.muni.key, []).append(school.id)

    geojson = _load_jalisco_map_geojson()
    for feature in geojson["features"]:
        muni_key = feature["properties"]["mun_key"]
        school_ids = school_ids_by_muni.get(muni_key, [])
        payload = _build_map_payload_for_schools(level, school_ids)
        factors_student = {
            item["factor"]: item["value"] for item in payload["student"]["factors"]
        }
        factors_teacher = {
            item["factor"]: item["value"] for item in payload["teacher"]["factors"]
        }
        feature["properties"].update(
            {
                "SH_st": payload["student"]["social_health"],
                "SH_te": payload["teacher"]["social_health"],
                "respondents_st": payload["student"]["respondent_count"],
                "respondents_te": payload["teacher"]["respondent_count"],
                "anxiety": factors_student.get("Ansiedad", -1),
                "anhedonia": factors_student.get("Anhedonia", -1),
                "anxiety_t": factors_teacher.get("Ansiedad", -1),
                "anhedonia_t": factors_teacher.get("Anhedonia", -1),
            }
        )
    return geojson

# Create your views here.
# LOGIN VIEWS
result_dic = {
    "SEC": [
        {
            "name": "Ansiedad",
            "question": [
                {"id": 17, "answer": ["A"]},
                {"id": 24, "answer": ["A", "C"]},
                {"id": 25, "answer": ["A", "C"]},
                {"id": 26, "answer": ["A", "B"]},
                {"id": 27, "answer": ["A", "C"]},
                {"id": 95, "answer": ["A"]},
                {"id": 97, "answer": ["A"]},
                {"id": 98, "answer": ["A"]},
                {"id": 99, "answer": ["A"]},
                {"id": 100, "answer": ["A"]},
                {"id": 101, "answer": ["A"]},
                {"id": 102, "answer": ["A"]},
                {"id": 107, "answer": ["A"]},
                {"id": 114, "answer": ["A"]},
            ],
            "prevalence_min": [5, 11],
            "prevalence_mid": [11, 21],
            "prevalence_high": [21, 101],
            "recomendation_min": "Después de la pandemia es una tasa esperada, aunque no es muy alta es significativa y requiere de campañas de prevención de la ansiedad a nivel general.",
            "recomendation_mid": "Esta tasa ya es preocupante porque nos habla de una población muy joven con niveles de ansiedad no esperados a su edad, por lo que se recomienda que además de las campañas de prevención, haya intervenciones más directas, talleres de manejo de la ansiedad, información sobre identificación de síntomas y a quién recurrir.",
            "recomendation_high": "Se recomienda tener en el plantel y/o municipio, servicios específicos de ayuda en el manejo de la ansiedad, así como grupos terapéuticos. Ya que la tasa a partir de esta cifra es muy alta y se considera un agravante o detonante para la presencia de otros factores de riesgo.",
        },
        {
            "name": "Anhedonia",
            "question": [
                {"id": 4, "answer": ["A"]},
                {"id": 81, "answer": ["D"]},
                {"id": 82, "answer": ["D"]},
                {"id": 83, "answer": ["D"]},
                {"id": 85, "answer": ["A"]},
                {"id": 105, "answer": ["A"]},
                {"id": 106, "answer": ["A"]},
                {"id": 107, "answer": ["A"]},
                {"id": 108, "answer": ["A"]},
            ],
            "prevalence_min": [5, 11],
            "prevalence_mid": [11, 21],
            "prevalence_high": [21, 31],
            "prevalence_veryhigh": [31, 101],
            "recomendation_min": "Es una tasa esperada por la etapa de la adolescencia y los cambios hormonales.",
            "recomendation_mid": "Es una tasa considerable donde conviene realizar campañas de actividades académicas, deportivas y/o culturales que despierten su curiosidad.",
            "recomendation_high": "Conviene realizar campañas sobre la anhedonia, ya que una de las secuelas de la pandemia ha sido la presencia de síntomas anhedónicos en adolescentes y jóvenes, es importante trabajar junto con campañas de actividades académicas, deportivas y culturales que despierten la curiosidad. Así como una intervención más directa para explorar las relaciones familiares y con los pares del alumnado que presenta esta tasa.",
            "recomendation_veryhigh": "Con esta tasa la intervención directa en el alumnado a través de talleres o dinámicas grupales debe ser la prioridad, así como las campañas de prevención donde se les eduque con relación a los síntomas de la anhedonia y los recursos de ayuda con los que cuenta el alumnado; ya que la manifestación de la anhedonia es un agravante o detonante de otras conductas de riesgo que pueden llevar al suicidio o al consumo de sustancias.",
        },
        {
            "name": "Depresión",
            "question": [
                {"id": 13, "answer": ["A"]},
                {"id": 19, "answer": ["C"]},
                {"id": 24, "answer": ["A", "C"]},
                {"id": 25, "answer": ["A", "C"]},
                {"id": 26, "answer": ["A"]},
                {"id": 90, "answer": ["B"]},
                {"id": 96, "answer": ["A"]},
                {"id": 103, "answer": ["A"]},
                {"id": 104, "answer": ["A"]},
                {"id": 105, "answer": ["A"]},
                {"id": 108, "answer": ["A"]},
                {"id": 109, "answer": ["A"]},
                {"id": 112, "answer": ["A"]},
                {"id": 113, "answer": ["A"]},
                {"id": 114, "answer": ["A"]},
                {"id": 115, "answer": ["A"]},
            ],
            "prevalence_min": [5, 11],
            "prevalence_mid": [11, 21],
            "prevalence_high": [21, 31],
            "prevalence_veryhigh": [31, 101],
            "recomendation_min": "Es una tasa esperada por la etapa de la adolescencia y los cambios hormonales.",
            "recomendation_mid": "Es una tasa considerable donde conviene realizar campañas de actividades académicas, deportivas y/o culturales que despierten su curiosidad.",
            "recomendation_high": "Conviene realizar campañas sobre la depresión, ya que una de las secuelas de la pandemia ha sido la presencia de síntomas depresivos en adolescentes y jóvenes, es importante trabajar junto con campañas de actividades académicas, deportivas y culturales que despierten la curiosidad. Así como una intervención más directa para explorar las relaciones familiares y con los pares del alumnado que presenta esta tasa.",
            "recomendation_veryhigh": "Con esta tasa la intervención directa en el alumnado a través de talleres o dinámicas grupales debe ser la prioridad, así como las campañas de prevención donde se les eduque con relación a los síntomas de la depresión y los recursos de ayuda con los que cuenta el alumnado; ya que la manifestación de la anhedonia es un agravante o detonante de otras conductas de riesgo que pueden llevar al suicidio o al consumo de sustancias.",
        },
        {
            "name": "Vandalismo",
            "question": [
                {"id": 44, "answer": ["A"]},
                {"id": 45, "answer": ["A", "D", "E"]},
                {"id": 46, "answer": ["A", "D", "E"]},
            ],
            "prevalence_min": [5, 16],
            "prevalence_mid": [16, 26],
            "prevalence_high": [26, 101],
            "recomendation_min": "Es una cifra esperada, ya que es un comportamiento que está muy normalizado a esta edad a nivel global y que se considera una forma de expresión. Lo que no implica que no deba tener consecuencias. Lo mejor es la prevención general donde se exponga el reglamento de la institución y las posibles consecuencias al cometer estos actos, dependiendo del lugar donde sean llevados a cabo.",
            "recomendation_mid": "Estaríamos hablando de una conducta pre-delictiva que empieza a normalizarse y que refleja que empiezan a conformarse pequeños grupos vandálicos en la comunidad, por lo que es necesario hacer intervención directa con las personas que son descubiertas realizando reiteradamente esta actividad, de tal manera que se pueda redirigir a estos grupos hacia una participación más prosocial, evitando que empiecen carreras delictivas más adelante.",
            "recomendation_high": "Este porcentaje es un indicador de que en gran parte de la población hay una normalización de esta conducta que, aunque es predelictiva, refleja que hay un desarrollo y crecimiento de grupos vandálicos que pueden evolucionar en el inicio de carreras delictivas, por lo que se recomienda hacer campañas de persuasión y reconocimiento a las personas que no ejecutan estos comportamientos para dispersar estos grupos. Por otro lado, se recomienda también trabajar en su integración a la comunidad a través de conductas prosociales.",
        },
        {
            "name": "Infracciones contra la propiedad",
            "question": [
                {"id": 47, "answer": ["A"]},
                {"id": 48, "answer": ["A", "D", "E"]},
                {"id": 49, "answer": ["A", "D", "E"]},
            ],
            "prevalence_min": [5, 16],
            "prevalence_mid": [16, 26],
            "prevalence_high": [26, 101],
            "recomendation_min": "Esta prevalencia es un indicador de la presencia de una conducta delictiva que puede detonarse como mera experimentación o por retos de parte de los pares y que puede redirigirse a una conducta prosocial. Por lo que es imperante fomentar la cultura de legalidad aplicando las sanciones adecuadas para que la conducta sea inhibida y no crezca convirtiéndose en un problema social.",
            "recomendation_mid": "Esta cifra se acerca a la normalización de la conducta, lo que es un indicador de una cultura de legalidad escasa que deja ver una permisividad en el comportamiento delictivo de la población más joven, lo que a tan temprana edad puede derivar en una carrera delictiva sin que se asuma algún tipo de responsabilidad. De ahí que se sugiera una escuela de padres donde se les oriente en la comunicación entre adultos y adolescentes, identificación de conductas delictivas y predelictivas y cómo afrontarlo. De igual manera es importante generar normativas que promuevan la cultura de legalidad en la comunidad y aplicar consecuencias que realmente generen una conciencia en la población joven para lograr reducir esta cifra antes de que consoliden una carrera delictiva.",
            "recomendation_high": "Un porcentaje tan alto es un indicador de que la conducta está más que normalizada y de que existen grupos que ya están afianzando una carrera delictiva como estilo de vida. Pero al ser una población tan joven es posible revertir ese porcentaje y enseñarles habilidades sociales para que se vinculen con la comunidad y actúen de manera prosocial. De igual manera es importante desarrollar programas para que los adultos que forman parte de la comunidad confíen en lo que la juventud puede aportar y fomente el diálogo y la participación en la construcción de una buena salud social.",
        },
        {
            "name": "Consumo de estupefacientes (Drogas blandas)",
            "question": [
                {"id": 61, "answer": ["A"]},
                {"id": 62, "answer": ["A"]},
                {"id": 63, "answer": ["A"]},
                {"id": 64, "answer": ["A"]},
                {"id": 65, "answer": ["A"]},
                {"id": 66, "answer": ["A"]},
                {"id": 67, "answer": ["A", "E"]},
                {"id": 68, "answer": ["B", "E"]},
            ],
            "prevalence_min": [5, 16],
            "prevalence_mid": [16, 26],
            "prevalence_high": [26, 101],
            "recomendation_min": "Esta tasa de prevalencia indica que hay permisividad por parte de los adultos de la comunidad en el consumo de drogas blandas, como pueden ser el alcohol, tabaco y marihuana a edades muy tempranas. También nos indica que por parte de la población más joven empieza a haber una normalización en el consumo, por lo que es recomendable desarrollar programas enfocados a informar y experimentar sobre las consecuencias del consumo de alcohol tanto a la población más joven como a la población adulta. Para dejar de normalizarlo.",
            "recomendation_mid": "Esta tasa de prevalencia, indica permisividad y normalización de la conducta por parte de la población más joven y de gran parte de la comunidad adulta; Es recomendable desarrollar programas enfocados a informar y experimentar sobre las consecuencias del consumo de alcohol tanto a la población más joven como a la población adulta. Más con un enfoque lúdico que aleccionador, de tal manera que se sientan con la libertad de expresar sus inquietudes y razones por las cuales consumen y les permita dejar de normalizar el consumo.",
            "recomendation_high": "Una tasa de prevalencia tan alta indica que además de normalización y permisividad, puede existir un refuerzo social al consumo a edades tan tempranas. Es recomendable desarrollar programas enfocados a informar y experimentar sobre las consecuencias del consumo de alcohol tanto a la población más joven como a la población adulta. Más con un enfoque lúdico que aleccionador, de tal manera que se sientan con la libertad de expresar sus inquietudes y razones por las cuales consumen y les permita dejar de normalizar el consumo. También es indispensable diseñar protocolos de acción dentro de los centros escolares y/o ayuntamientos, para lograr inhibir el consumo de alcohol, tabaco y marihuana. Así como ofrecer formación a los adultos del contexto inmediato para formar un mismo frente y evitar que se convierta en la puerta hacia otras conductas de riesgo.",
        },
        {
            "name": "Ideación suicida",
            "question": [
                {"id": 25, "answer": ["A", "C"]},
                {"id": 28, "answer": ["A", "B", "C"]},
                {"id": 110, "answer": ["A"]},
                {"id": 111, "answer": ["A"]},
                {"id": 112, "answer": ["A"]},
            ],
            "prevalence_min": [1, 6],
            "prevalence_mid": [6, 11],
            "prevalence_high": [11, 101],
            "recomendation_min": "Esta tasa de prevalencia en este rango de edad se puede explicar por efectos de la pandemia, sin embargo, esto no implica que sea totalmente así. Por lo que se recomienda abrir espacios donde se hale abiertamente del tema, sin prejuicios ni miedos, espacios donde puedan expresar como se sienten sin ser juzgados. También se recomienda diseñar protocolos de intervención escolar tanto con padres como con el profesorado para que puedan acompañar al alumnado que está pasando por una ideación suicida. Es recomendable que existan líneas telefónicas de apoyo o que existan grupos a los que puedan acudir en busca de ayuda.",
            "recomendation_mid": "Esta tasa de prevalencia es una alarma importante que no debe pasar desapercibida. Se recomienda líneas telefónicas de apoyo y que existan grupos a los que puedan acudir en busca de ayuda. Que se diseñen protocolos de seguridad para que el alumnado que sufre esta ideación suicida no corra el riesgo de consumarlo, lo más importante es hablar del tema en el entorno escolar, con el alumnado, que aprendan a identificar síntomas, que sepan a dónde pueden acudir y cómo pueden ser un apoyo emocional en caso de que reconozcan los síntomas en algún compañero o compañera. Hacer grupos de conciencia social donde todos cuidan de todos.",
            "recomendation_high": "Es una tasa de prevalencia alta, que requiere de intervención y prevención en todas las esferas de la vida de la población más joven, lo que implica trabajar y formar a los adultos del entorno inmediato (padres, profesorado y personal administrativo) en la detección de este comportamiento y en cómo abordar el problema si se identifica en casa o en el centro escolar. También se vuelve indispensable tener líneas de ayuda, grupos de apoyo emocional y que se hable abiertamente del tema en el entorno escolar, para que de esta manera sepan identificar los síntomas, sepan a dónde recurrir y cómo apoyar a un compañero o compañera que puede estar manifestando señales de ideación suicida.",
        },
        {
            "name": "Adicción a las redes sociales",
            "question": [
                {"id": 32, "answer": ["B"]},
                {"id": 33, "answer": ["B"]},
                {"id": 35, "answer": ["B", "C"]},
                {"id": 36, "answer": ["C", "D", "E"]},
                {"id": 37, "answer": ["C", "D"]},
            ],
            "prevalence_min": [10, 26],
            "prevalence_mid": [26, 36],
            "prevalence_high": [36, 101],
            "recomendation_min": "Esta conducta está muy normalizada a edades muy tempranas, por los que es importante abrir espacios de ocio alternativo para que los más jóvenes socialicen sin depender de la tecnología. Y es recomendable desarrollar programas de divulgación en los que se informe, a los padres, madres, tutores, personal del centro escolar y a la población más joven, sobre los efectos del uso excesivo de las redes sociales y el impacto que tienen en el auto-concepto y autoestima de las personas, sin perder de vista que, al convertirse en una adicción, se activan los mismos mecanismos a nivel cerebral como en cualquier otra adicción.",
            "recomendation_mid": "Con esta tasa de prevalencia, en este rango de edad, se recomienda abrir espacios de ocio alternativo para que vuelvan a socializar sin depender de la tecnología, así como desarrollar programas de concientización sobre la adicción a las redes sociales y se realicen talleres donde tanto adultos como el alumnado puedan identificar señales de una adicción a las redes sociales y conozcan los recursos con los que cuentan para conseguir ayuda, en caso de que sea un alumno o alumna o algún adulto que identifique estos síntomas en un hijo o hija o en el alumnado. ",
            "recomendation_high": "Con una tasa de prevalencia tan alta, es recomendable abrir grupos de apoyo para el manejo de la adicción a las redes sociales, así como contar con programas que desarrollen una autoestima sana, ya que esta adicción suele tener un gran impacto en la autoestima, lo que puede hacer que generen otro tipo de comportamientos y síntomas, como ansiedad y depresión, así como trastornos de alimentación. También es necesario abrir espacios de ocio alternativo para que vuelvan a socializar sin depender de la tecnología.",
        },
        {
            "name": "Control de impulsos",
            "question": [
                {"id": 23, "answer": ["A", "C"]},
            ],
            "prevalence_min": [10, 21],
            "prevalence_mid": [21, 31],
            "prevalence_high": [31, 101],
            "recomendation_min": "Esta tasa de prevalencia en menores de 15 años es esperable, ya que es cuando inicia la adolescencia. Por lo que se recomienda desarrollar programas donde se trabaje la toma de decisiones, y se les enseñe a reflexionar sobre compartir ciertos contenidos online, el tipo de respuestas que dan ante ciertas situaciones y la asertividad.",
            "recomendation_mid": "Esta tasa de prevalencia en menores de 15 años indica que ha habido cierta permisividad en algunos comportamientos que servirían para entrenar el autocontrol y no los ha habido, lo que ha disminuido esta capacidad en su preadolescencia. Por lo que se recomienda desarrollar talleres sobre el autocontrol y manejo de la frustración, así como programas donde se trabaje la toma de decisiones, y se les enseñe a reflexionar sobre compartir ciertos contenidos online, el tipo de respuestas que dan ante ciertas situaciones y la asertividad. Esto con el fin de evitar que caigan en otras conductas de riesgo que son detonadas por la falta de autocontrol como puede ser el consumo de estupefacientes, la adicción a las redes sociales y el uso de la violencia física como resolución de un conflicto.",
            "recomendation_high": "Una tasa de prevalencia que abarca más de la tercera parte de la población más joven, es una llamada de atención para toda la comunidad, ya que pone en evidencia que los mecanismos de autocontrol que deberían proporcionarse en la infancia y preadolescencia, no se están entrenando, por lo que se recomienda desarrollar grupos o escuelas para padres para proporcionarles las herramientas que permitirán fomentar el control de impulsos de la población más joven. Así como es recomendable desarrollar talleres sobre el autocontrol y manejo de la frustración, así como programas donde se trabaje la toma de decisiones, y se les enseñe a reflexionar sobre compartir ciertos contenidos online, el tipo de respuestas que dan ante ciertas situaciones y la asertividad.",
        },
        {
            "name": "Consumo de estupefacientes (Drogas duras)",
            "question": [
                {"id": 69, "answer": ["A"]},
                {"id": 70, "answer": ["A"]},
                {"id": 71, "answer": ["A"]},
                {"id": 72, "answer": ["A"]},
                {"id": 73, "answer": ["A"]},
                {"id": 74, "answer": ["A", "E"]},
                {"id": 75, "answer": ["A"]},
                {"id": 76, "answer": ["A"]},
                {"id": 77, "answer": ["A", "E"]},
                {"id": 78, "answer": ["B", "C"]},
                {"id": 79, "answer": ["B", "C"]},
            ],
            "prevalence_min": [1, 6],
            "prevalence_mid": [6, 11],
            "prevalence_high": [11, 101],
            "recomendation_min": "Esta tasa además de ser significativa, expone la normalización que está teniendo el consumo de drogas duras en el plantel y nos habla de su cultura de legalidad. Se recomienda desarrollar grupos donde la población más joven pueda hablar abiertamente sobre los efectos que tienen las drogas duras en el organismo y en su salud mental, así como resolver las dudas que puedan tener sin emitir juicios de valor.",
            "recomendation_mid": "Esta tasa de prevalencia, es un indicador de la permisividad que está teniendo entre el plantel el consumo de drogas duras y nos habla de su cultura de legalidad. Se recomienda desarrollar grupos donde la población más joven pueda hablar abiertamente sobre los efectos que tienen las drogas duras en el organismo y en su salud mental, así como resolver las dudas que puedan tener sin emitir juicios de valor. El desarrollo de cursos para padres en los que se enseñe a identificar el consumo de drogas duras en sus hijos y cómo actuar o a donde recurrir.",
            "recomendation_high": "Una tasa de prevalencia tan alta, es una señal de alerta que indica la facilidad en el acceso a las drogas duras y la alta permisividad en el entorno inmediato de la población más joven. Así como de algunos grupos que ya presentan el consumo como un hábito, lo que puede significar que muy posiblemente también participen en otro tipo de conductas de riesgo como el consumo de alcohol, violencia física y violaciones contra la propiedad. Se recomienda desarrollar grupos donde la población más joven pueda hablar abiertamente sobre los efectos que tienen las drogas duras en el organismo y en su salud mental, así como resolver las dudas que puedan tener sin emitir juicios de valor. El desarrollo de cursos para padres en los que se enseñe a identificar el consumo de drogas duras en sus hijos y cómo actuar o a donde recurrir, debiendo existir consecuencias claras para las personas que consumen reiteradamente dentro y fuera del plantel, con independencia de la edad con el fin de inhibir este comportamiento y aumentar la cultura de legalidad.",
        },
        {
            "name": "Violencia",
            "question": [
                {"id": 51, "answer": ["A"]},
                {"id": 52, "answer": ["A", "D", "E"]},
                {"id": 53, "answer": ["A", "E"]},
                {"id": 54, "answer": ["A"]},
                {"id": 55, "answer": ["A", "D", "E"]},
                {"id": 56, "answer": ["A", "D"]},
                {"id": 57, "answer": ["A"]},
                {"id": 50, "answer": ["A"]},
                {"id": 58, "answer": ["A"]},
                {"id": 59, "answer": ["A", "D", "E"]},
                {"id": 60, "answer": ["B", "C"]},
            ],
            "prevalence_min": [5, 16],
            "prevalence_mid": [16, 26],
            "prevalence_high": [26, 101],
            "recomendation_min": "Esta tasa de prevalencia es un indicador de la normalización que está teniendo la violencia física como recurso en la interacción social desde edades muy tempranas, por lo que se recomienda desarrollar programas de habilidades sociales, resolución de conflictos y tener una normativa clara con sus consecuencias, así como una escuela para padres donde se les pueda  formar y poner en conocimiento sobre las herramientas que existen y como utilizarlas en una comunidad socialmente sana y segura. Utilizando como eje la primicia de que la violencia no es una alternativa de socialización dentro de la población.",
            "recomendation_mid": "Esta tasa de prevalencia es un indicador de que existe tolerancia a la violencia porque se manifiesta como una manera de interacción dentro de una parte importante de la población más joven; ya sea por indiferencia de la población adulta o la normalización de esta conducta por parte de toda la comunidad. Por lo que es necesario promover actividades y desarrollar programas que tengan que ver con habilidades sociales, mediación, alternativas de ocio y normativas claras con sus consecuencias. De tal manera que se puedan realizar campañas informativas sobre estas para que la población vea que la comunidad no tolerará este tipo de conductas predelictivas. Mostrándoles que existen alternativas para dialogar o mediar sin llegar al uso de la violencia física. Lo que hará que la comunidad entera desarrolle una cultura hacia la no violencia. Y disminuya la normalización de la violencia física como herramienta de resolución de conflictos.",
            "recomendation_high": "Una tasa de prevalencia tan alta indica una alta tolerancia a la violencia por parte de la población. Ya no sólo una normalización, aquí la intervención debe ser directa con normas y consecuencias claras para la población joven que sigue este patrón. Para que toda la comunidad deje claro que no hay tolerancia alguna ante la violencia física, implica la formación de grupos violentos que pueden estar intimidando a una parte importante de la población estudiantil, generando un sentimiento de impotencia e inseguridad. Es. Importante proporcionar talleres para el manejo de la violencia, la frustración y desarrollar programas de habilidades sociales que generen conciencia en toda la población y que no solamente se escandalicen por sucesos violentos, sino que sean capaces de denunciarlos y apoyar desde la escuela o casa para paliar este comportamiento. Generando una nueva cultura hacia la no violencia.",
        },
        {
            "name": "Acoso escolar",
            "question": [
                {"id": 20, "answer": ["A"]},
                {"id": 21, "answer": ["A"]},
                {"id": 22, "answer": ["A"]},
            ],
            "prevalence_min": [5, 16],
            "prevalence_mid": [16, 26],
            "prevalence_high": [26, 101],
            "recomendation_min": "Con esta tasa de prevalencia es recomendable desarrollar programas de sensibilización y compromiso social para que los adultos que forman parte de la comunidad adopten un rol activo que ayude a prevenir este comportamiento. Es importante especificar el tipo de comportamientos que implican un acoso escolar, ya que muchas veces está tan normalizado que no se dan cuenta de que lo que están haciendo es acoso escolar. Por lo que la formación con relación a este tema es indispensable en todo el personal de la Institución Educativa.",
            "recomendation_mid": "Esta tasa de prevalencia es un indicador de que el acoso escolar empieza a normalizarse, tanto para los acosadores como para las víctimas, y proyecta cierta inacción de parte de la comunidad adulta de la comunidad. Por lo que se recomiendan talleres que inviten a participar a tanto a padres como a docentes, así como al alumnado para proponer formas de convivencia y de resolución de conflictos y sensibilización sobre el tema. También se recomienda tener claros los protocolos de acción del plantel y/o Ayuntamiento y ponerlos en conocimiento de la comunidad. Es importante especificar el tipo de comportamientos que implica el acoso escolar ya que muchas veces está tan normalizado que no se dan cuenta de que lo que están haciendo es acoso escolar. Por lo que la formación con relación a este tema es indispensable en todo el personal de la Institución Educativa.",
            "recomendation_high": "Esta tasa de prevalencia  es un indicador de la normalización del acoso escolar y de una inacción por parte de la comunidad adulta, por lo que además de tener protocolos de acción en los centros escolares e informar de ellos, es recomendable que se involucren también a las autoridades fuera del centro escolar y que trabajen en conjunto para lograr inhibir este comportamiento. Que los padres participen en talleres de sensibilización, que existan grupos de apoyo a las víctimas de acoso escolar, así como grupos para trabajar con acosadores, logrando una buena cobertura del acoso escolar.",
        },
        {
            "name": "Conductas antisociales",
            "question": [
                {"id": 38, "answer": ["A", "C"]},
                {"id": 39, "answer": ["A", "D", "E"]},
                {"id": 40, "answer": ["A"]},
                {"id": 41, "answer": ["B"]},
                {"id": 42, "answer": ["A", "D", "E"]},
                {"id": 43, "answer": ["A", "C"]},
            ],
            "prevalence_min": [5, 16],
            "prevalence_mid": [16, 26],
            "prevalence_high": [26, 101],
            "recomendation_min": "Esta tasa de prevalencia indica que probablemente este comportamiento se de por experimentación y no esté normalizado dentro de la población más joven, sin embargo, para evitar que llegue a adoptarse como un comportamiento normal, se recomienda desarrollar programas de participación social, de tal manera que se genere un vínculo de compromiso entre la población más joven y su comunidad, donde se trabaje el respeto a las normas de convivencia y la importancia de ser tomados en cuenta en las decisiones que implican a la juventud. ",
            "recomendation_mid": "Esta tasa de prevalencia es un indicador de que este comportamiento empieza a normalizarse y que puede estar reforzado por los pares, lo que puede hacer que lo adopten como estilo de vida. Por lo que se recomienda que se desarrollen programas donde se fomenten los vínculos con la comunidad y el compromiso social. Haciéndoles partícipes de las decisiones que toma la institución educativa y/o el Ayuntamiento que implican a la juventud. Como la creación de un bono o carnet joven que le permita hacer determinada cantidad de viajes gratuitos, descuentos en libros, permitiendo que se afilien establecimientos de todo tipo para que perciban las ventajas de seguir las reglas y se habitúen a realizar algunos trámites para obtener dicho carnet. Así evaluaran que el costo de no cumplir las normas es más alto que el beneficio. Así como trabajar con ellos en la propuesta de normas y consecuencias en caso de que actúen de esta manera.",
            "recomendation_high": "Una tasa de prevalencia tan alta indica una normalización de la conducta y permisividad ante este comportamiento por lo que implementar programas de conciencia social se hace indispensable, también se recomienda que se desarrollen programas donde se fomenten los vínculos con la comunidad y el compromiso social. Haciéndoles partícipes de las decisiones que toma la institución educativa y/o el Ayuntamiento que implican a la juventud. Como la creación de un bono o carnet joven que les permita hacer determinada cantidad de viajes gratuitos, descuentos en libros, permitiendo que se afilien establecimientos de todo tipo para que perciban las ventajas de seguir las reglas y se habitúen a realizar algunos trámites para obtener dicho carnet. Así evaluaran que el costo de no cumplir las normas es más alto que el beneficio.",
        },
        {
            "name": "Relación alumnado - plantel",
            "question": [
                {"id": 3, "answer": ["A", "B"]},
                {"id": 18, "answer": ["A", "B", "C"]},
                {"id": 80, "answer": ["A"]},
                {"id": 82, "answer": ["A", "B"]},
                {"id": 83, "answer": ["A", "B"]},
                {"id": 84, "answer": ["A"]},
                {"id": 88, "answer": ["A"]},
                {"id": 89, "answer": ["A"]},
                {"id": 90, "answer": ["A"]},
                {"id": 91, "answer": ["B"]},
                {"id": 92, "answer": ["B"]},
                {"id": 93, "answer": ["A"]},
                {"id": 94, "answer": ["A"]},
                {"id": 119, "answer": ["A"]},
                {"id": 120, "answer": ["A"]},
            ],
            "prevalence_min": [25, 101],
            "recomendation_min": "Es muy buena noticia saber que más de la cuarta parte de la población más joven se siente en su plantel con la confianza de moverse libremente y de recurrir al personal docente y administrativo en caso de tener algún problema. Así como saber que reconocen el trabajo del personal docente y a la vez se sienten valorados el personal del plantel.  Esto indica que el plantel está cumpliendo como agente de socialización. Lo que contribuye significativamente a la salud social del plantel y su entorno inmediato.",
        },
        {
            "name": "Familiares",
            "question": [
                {"id": 7, "answer": ["A", "B", "C"]},
                {"id": 8, "answer": ["D", "E"]},
                {"id": 9, "answer": ["D", "E"]},
                {"id": 10, "answer": ["B", "C"]},
                {"id": 11, "answer": ["B"]},
                {"id": 12, "answer": ["B"]},
                {"id": 116, "answer": ["B"]},
            ],
            "prevalence_min": [35, 101],
            "recomendation_min": "Cuando más de la tercera parte de la población más joven cuenta con una buena relación familiar, comunican con quién y a dónde van cuando salen de casas y/o cuentan con padres que han estudiado; es un indicador determinante a la hora de paliar con los factores de riesgo que existen dentro y fuera del plantel. Ya que es un factor protector vital que impacta directamente en el entorno inmediato debido a que este factor es el que determina el sistema de valores y creencias de los más jóvenes, así como su cultura de legalidad y la manera como se relacionarán con el entorno.",
        },
        {
            "name": "Personales",
            "question": [
                {"id": 13, "answer": ["B", "C", "D", "E"]},
                {"id": 19, "answer": ["A", "B"]},
                {"id": 85, "answer": ["A"]},
                {"id": 86, "answer": ["B"]},
                {"id": 118, "answer": ["A"]},
            ],
            "prevalence_min": [30, 101],
            "recomendation_min": "En la población más joven, pasar la mayor parte del tiempo en compañía, considerar la importancia de estudiar, tener un buen auto-concepto con relación a los demás, así como sentirse bien con su género son factores protectores determinantes en el desarrollo durante la pubertad. Que una tercera parte de la población más joven o más, reconozca estos factores en su cuestionario, no solamente nos habla de una parte de la población que se mueve con seguridad en su plantel y en su entorno, sino que además indica que existe la posibilidad de que estos factores al determinar parte de la manera como se relacionan, se puedan reproducir en el alumnado que carece de estos factores protectores, debido a la socialización Incentivando la salud social dentro y fuera del plantel de la población preadolescente y adolescente.",
        },
    ],
    "EMS": [
        {
            "name": "Ansiedad",
            "question": [
                {"id": 17, "answer": ["A"]},
                {"id": 24, "answer": ["A", "C"]},
                {"id": 25, "answer": ["A", "C"]},
                {"id": 26, "answer": ["A", "B"]},
                {"id": 27, "answer": ["A", "C"]},
                {"id": 95, "answer": ["A"]},
                {"id": 97, "answer": ["A"]},
                {"id": 98, "answer": ["A"]},
                {"id": 99, "answer": ["A"]},
                {"id": 100, "answer": ["A"]},
                {"id": 101, "answer": ["A"]},
                {"id": 102, "answer": ["A"]},
                {"id": 107, "answer": ["A"]},
                {"id": 114, "answer": ["A"]},
            ],
            "prevalence_min": [5, 11],
            "prevalence_mid": [11, 26],
            "prevalence_high": [26, 101],
            "recomendation_min": "Después de la pandemia es una tasa esperada, tomando en cuenta la edad y las inquietudes que viven con relación al futuro, aunque no es muy alta es significativa y requiere de campañas de prevención de la ansiedad a nivel general. ",
            "recomendation_mid": "Esta tasa ya es preocupante porque nos habla de una población muy joven con niveles de ansiedad no esperados a su edad, por lo que se recomienda que además de las campañas de prevención, haya intervenciones más directas, talleres de manejo de la ansiedad, información sobre identificación de síntomas y a quién recurrir.",
            "recomendation_high": "Se recomienda tener en el plantel y/o municipio, servicios específicos de ayuda en el manejo de la ansiedad, así como grupos terapéuticos. Ya que la tasa a partir de esta cifra es muy alta y se considera un agravante o detonante para la presencia de otros factores de riesgo.",
        },
        {
            "name": "Anhedonia",
            "question": [
                {"id": 4, "answer": ["A"]},
                {"id": 81, "answer": ["D"]},
                {"id": 82, "answer": ["D"]},
                {"id": 83, "answer": ["D"]},
                {"id": 85, "answer": ["A"]},
                {"id": 105, "answer": ["A"]},
                {"id": 106, "answer": ["A"]},
                {"id": 107, "answer": ["A"]},
                {"id": 108, "answer": ["A"]},
            ],
            "prevalence_min": [5, 11],
            "prevalence_mid": [11, 26],
            "prevalence_high": [26, 101],
            "recomendation_min": "La tasa es esperada debido al efecto de la pandemia, pero también es significativa, por lo que se recomienda realizar campañas de actividades académicas, deportivas y/o culturales que despierten su curiosidad.",
            "recomendation_mid": "La tasa de prevalencia es alta por lo que es importante, además, de realizar campañas sobre actividades académicas, deportivas y culturales, intervenir directamente con talleres sobre la anhedonian. Así como proporcionando información sobre los síntomas anhedónicos y sobre los recursos de ayuda con los que cuenta el alumnado a nivel plantel y/o municipal. También es indispensable una intervención más directa para explorar las relaciones familiares y con los pares del alumnado que presentan esta tasa.",
            "recomendation_high": "Con esta tasa de prevalencia la intervención directa en el alumnado a través de talleres o dinámicas grupales debe ser la prioridad, así como las campañas de prevención donde se les eduque con relación a los síntomas de la anhedonia y los recursos de ayuda con los que cuenta el alumnado; ya que la manifestación de la anhedonia es un agravante o detonante de otras conductas de riesgo que pueden llevar al suicidio o al consumo de sustancias.",
        },
        {
            "name": "Depresión",
            "question": [
                {"id": 13, "answer": ["A"]},
                {"id": 19, "answer": ["C"]},
                {"id": 24, "answer": ["A", "C"]},
                {"id": 25, "answer": ["A", "C"]},
                {"id": 26, "answer": ["A"]},
                {"id": 90, "answer": ["B"]},
                {"id": 96, "answer": ["A"]},
                {"id": 103, "answer": ["A"]},
                {"id": 104, "answer": ["A"]},
                {"id": 105, "answer": ["A"]},
                {"id": 108, "answer": ["A"]},
                {"id": 109, "answer": ["A"]},
                {"id": 112, "answer": ["A"]},
                {"id": 113, "answer": ["A"]},
                {"id": 114, "answer": ["A"]},
                {"id": 115, "answer": ["A"]},
            ],
            "prevalence_min": [5, 11],
            "prevalence_mid": [11, 26],
            "prevalence_high": [26, 101],
            "recomendation_min": "La tasa es esperada debido al efecto de la pandemia, pero también es significativa, por lo que se recomienda realizar campañas de actividades académicas, deportivas y/o culturales que despierten su curiosidad.",
            "recomendation_mid": "La tasa de prevalencia es alta por lo que es importante, además, de realizar campañas sobre actividades académicas, deportivas y culturales, intervenir directamente con talleres sobre la depresión. Así como proporcionando información sobre los síntomas de depresión y sobre los recursos de ayuda con los que cuenta el alumnado a nivel plantel y/o municipal. También es indispensable una intervención más directa para explorar las relaciones familiares y con los pares del alumnado que presentan esta tasa.",
            "recomendation_high": "Con esta tasa de prevalencia la intervención directa en el alumnado a través de talleres o dinámicas grupales debe ser la prioridad, así como las campañas de prevención donde se les eduque con relación a los síntomas de la depresión y los recursos de ayuda con los que cuenta el alumnado; ya que la manifestación de la depresión es un agravante o detonante de otras conductas de riesgo que pueden llevar al suicidio o al consumo de sustancias.",
        },
        {
            "name": "Vandalismo",
            "question": [
                {"id": 44, "answer": ["A"]},
                {"id": 45, "answer": ["A", "D", "E"]},
                {"id": 46, "answer": ["A", "D", "E"]},
            ],
            "prevalence_min": [10, 21],
            "prevalence_mid": [21, 31],
            "prevalence_high": [31, 101],
            "recomendation_min": "A estas edades es cuando más se presenta esta conducta predelictiva como una forma de rebeldía o disconformidad, por lo que es importante abordarla con campañas de persuasión como puede ser dar a conocer el reglamento de la institución que esté sufriendo estos percances. Y dando alternativas de ocio y de actividades prosociales a los jóvenes de la comunidad. Sin ejercer la represión absoluta, ya que esto genera una resistencia más fuerte hacia las normas sociales.",
            "recomendation_mid": "En este rango de edad, este porcentaje, es el parteaguas en esta conducta predelictiva en concreto, es aquí donde se tienen que tomar medidas de intervención y de persuasión. Ya no sólo poner en su conocimiento las consecuencias y lo que implica este comportamiento, sino que hay que intervenir aplicando la normativa con la que se cuenta o hacer una nueva que persuada esta conducta predelictiva y promueva conductas de integración prosocial, ya que es un indicador de la formación de grupos vandálicos dentro de la comunidad, grupos que abren la puerta hacia una carrera delictiva.",
            "recomendation_high": "Esta tasa de prevalencia tan alta, es un indicador de que la conducta predelictiva esta normalizada por la comunidad, hay resignación y aceptación, por lo que es importante trabajar con talleres y/o actividades comunitarias que afiancen los lazos de esta población con su comunidad. Una tasa de prevalencia alta, también es un indicador de que existen grupos que empiezan a afianzar una posible carrera delictiva.",
        },
        {
            "name": "Infracciones contra la propiedad",
            "question": [
                {"id": 47, "answer": ["A"]},
                {"id": 48, "answer": ["A", "D", "E"]},
                {"id": 49, "answer": ["A", "D", "E"]},
            ],
            "prevalence_min": [5, 16],
            "prevalence_mid": [16, 26],
            "prevalence_high": [26, 101],
            "recomendation_min": "En este rango de edad es cuando se consolidan muchas conductas de riesgo por lo que es indispensable que se desarrollen programas para establecer vínculos entre la comunidad y la población joven que presenta estas características. Dichas actividades deberían ir dirigidas a la población en general para evitarla estigmatización, ya que el propósito es que este porcentaje de la población se integre con conductas prosociales sin poner en evidencia las conductas de riesgo que los colocan dentro de esta cifra. De igual manera es importante reforzar con alguna campaña o taller la cultura de legalidad que maneja la comunidad.",
            "recomendation_mid": "Al ser una edad en la que se consolidan las conductas de riesgo y en la que se puede comenzar una carrera delictiva, es importante intervenir de manera directa en la población y promover talleres donde la misma población trabaje y haga conciencia de su cultura de legalidad y no permita la normalización de conductas delictivas a través de la resignación. Por lo que es importante reinventar la relación de las y los jóvenes con su comunidad. Proporcionarles herramientas y espacios donde los adultos que forman parte de la comunidad confíen en lo que la juventud puede aportar y fomente el diálogo y la participación en la construcción de una buena salud social.",
            "recomendation_high": "Con un porcentaje tan alto en una edad en la que se consolidan las conductas de riesgo y dan comienzo las carreras delictivas, se puede ver que la conducta está más que normalizada y de que existen grupos comienzan a afianzar una carrera delictiva como estilo de vida. Es indispensable que se desarrollen programas para establecer vínculos entre la comunidad y la población joven que presenta estas características. Es importante reinventar la relación de las y los jóvenes con su comunidad. Proporcionar herramientas y espacios donde los adultos que forman parte de la comunidad confíen en lo que la juventud puede aportar y fomente el diálogo y la participación en la construcción de una buena salud social.",
        },
        {
            "name": "Consumo de estupefacientes (Drogas blandas)",
            "question": [
                {"id": 61, "answer": ["A"]},
                {"id": 62, "answer": ["A"]},
                {"id": 63, "answer": ["A"]},
                {"id": 64, "answer": ["A"]},
                {"id": 65, "answer": ["A"]},
                {"id": 66, "answer": ["A"]},
                {"id": 67, "answer": ["A", "E"]},
                {"id": 68, "answer": ["A", "E"]},
            ],
            "prevalence_min": [10, 26],
            "prevalence_mid": [26, 36],
            "prevalence_high": [36, 101],
            "recomendation_min": "Esta tasa de prevalencia indica que hay permisividad por parte de los adultos de la comunidad en el consumo de drogas blandas, como pueden ser el alcohol, tabaco y marihuana. También nos indica que empieza a haber una normalización en el consumo, por lo que es recomendable desarrollar programas enfocados a informar y experimentar sobre las consecuencias del consumo de alcohol tanto en la salud como en lo social. Dicha formación es recomendable que se dé tanto al alumnado como a la población adulta. Para dejar de normalizarlo.",
            "recomendation_mid": "Esta tasa de prevalencia, indica permisividad y normalización de la conducta por parte de la población joven y de gran parte de la comunidad adulta. Es recomendable desarrollar programas enfocados a informar y experimentar sobre las consecuencias del consumo de alcohol tanto a la población más joven como a la población adulta. Más con un enfoque lúdico que aleccionador, de tal manera que se sientan con la libertad de expresar sus inquietudes y razones por las cuales consumen y les permita dejar de normalizar el consumo.",
            "recomendation_high": "Una tasa de prevalencia tan alta, indica que además de normalización y permisividad, puede existir un refuerzo social de parte de los pares al consumo de alcohol, tabaco y/o marihuana. Es recomendable desarrollar programas enfocados a informar y experimentar sobre las consecuencias del consumo de alcohol tanto a la población joven como a la población adulta. Trabajar con un enfoque lúdico y no aleccionador, de tal manera que se sientan con la libertad de expresar sus inquietudes y razones por las cuales consumen y les permita dejar de normalizar el consumo. También es indispensable diseñar protocolos de acción dentro de los centros escolares y/o ayuntamientos, para lograr inhibir el consumo.",
        },
        {
            "name": "Ideación suicida",
            "question": [
                {"id": 25, "answer": ["A", "C"]},
                {"id": 28, "answer": ["A", "B", "C"]},
                {"id": 110, "answer": ["A"]},
                {"id": 111, "answer": ["A"]},
                {"id": 112, "answer": ["A"]},
            ],
            "prevalence_min": [1, 6],
            "prevalence_mid": [6, 11],
            "prevalence_high": [11, 101],
            "recomendation_min": "Esta tasa de prevalencia es una señal importante de alerta. Siendo este rango de edad en donde más suicidios se cometen y donde más difícil se hace la intervención porque se tiene la idea de que los adolescentes a esas edades no necesitan del acompañamiento de un adulto. Es imperante que en las instituciones educativas se hable abiertamente del tema, sin tabúes, que se forme a todo el entorno escolar sobre los síntomas, y los recursos con los que cuenta.",
            "recomendation_mid": "Una tasa de prevalencia alta en este factor de riesgo implica que falta formación con relación al suicidio, y que hay una falta importante de recursos o de información sobre qué hacer si se tiene esta conducta o si conocemos a alguien que presenta ideación suicida. Por lo que es imperante hablar abiertamente en el entorno escolar sobre el suicidio, los recursos, cómo servir de apoyo emocional en lo que se activan protocolos de seguridad, y sobre todo en la no banalización del tema cuando se trata de alumnado de este rango de edad, ya que la cifra se pudo haber visto aumentada como una consecuencia de la pandemia.",
            "recomendation_high": "Esta tasa implicaría un fracaso con relación al acompañamiento de la juventud. Donde urgiría hacer intervenciones directas y concientizar a toda la población con relación al suicidio. Implicaría que se hablara abiertamente del tema en todos los entornos, sin prejuicios, por el bien común, quitando el morbo y convirtiéndonos todos en agentes de apoyo emocional para los jóvenes de la comunidad, además de implementar líneas de ayuda y grupos de apoyo.",
        },
        {
            "name": "Adicción a las redes sociales",
            "question": [
                {"id": 32, "answer": ["B"]},
                {"id": 33, "answer": ["B"]},
                {"id": 35, "answer": ["B", "C"]},
                {"id": 36, "answer": ["C", "D", "E"]},
                {"id": 37, "answer": ["C", "D"]},
            ],
            "prevalence_min": [10, 26],
            "prevalence_mid": [26, 36],
            "prevalence_high": [36, 101],
            "recomendation_min": "Esta tasa de prevalencia es significativa. Por lo que es recomendable desarrollar programas dirigidos a la autoestima donde se forme a los jóvenes con relación al uso adecuado de las redes sociales y sus efectos a nivel cerebral y psicológico cuando se utilizan en exceso. Y abrir espacios de ocio alternativo para que vuelvan a socializar sin depender de la tecnología.",
            "recomendation_mid": "Esta tasa de prevalencia es esperada después de la pandemia, ya que gran parte de la socialización de los adolescentes se tuvo que realizar a través de las redes sociales, lo que ha normalizado pasar muchas horas ante las redes sociales velando una posible adicción. Es recomendable desarrollar programas de manejo de adicciones a las redes sociales dentro de las Instituciones educativas ya que la adicción a las redes sociales no solamente afecta el rendimiento escolar, sino que tiene un impacto en la salud mental, afecta la autoestima, el autoconcepto y el sistema nervioso, ya que el tipo de luz interfiere en el ciclo circadiano, provocando problemas para dormir y ansiedad. Lo que repercute en otras conductas de riesgo. Se vuelve indispensable abrir espacios de ocio alternativo para que vuelvan a socializar sin depender de la tecnología.",
            "recomendation_high": "Al ser una tasa de prevalencia tan alta la mejor alternativa es hacer a los adolescentes partícipes de las propuestas de ocio alternativo, dentro y fuera de la institución escolar, que propongan campañas donde hablen de lo difícil que es soltar las redes sociales y cómo les afecta. También es recomendable desarrollar programas de manejo de adicciones a las redes sociales dentro de las Instituciones educativas ya que la adicción a las redes sociales no solamente afecta el rendimiento escolar, sino que tiene un impacto en la salud mental, afecta la autoestima, el autoconcepto y el sistema nervioso, ya que el tipo de luz interfiere en el ciclo circadiano, provocando problemas para dormir y ansiedad. Lo que repercute en otras conductas de riesgo.",
        },
        {
            "name": "Control de impulsos",
            "question": [
                {"id": 23, "answer": ["A", "C"]},
            ],
            "prevalence_min": [10, 21],
            "prevalence_mid": [21, 31],
            "prevalence_high": [31, 101],
            "recomendation_min": "Esta tasa de prevalencia en este rango de edad es una llamada de atención para toda la comunidad, ya que pone en evidencia que los mecanismos de autocontrol que deberían proporcionarse en la infancia y preadolescencia, no se enseñaron, por lo que se recomienda desarrollar grupos o escuelas para padres para proporcionarles las herramientas que permitirán fomentar el control de impulsos en los adolescentes. Así como es recomendable desarrollar talleres de comunicación y autocontrol con adolescentes y manejo de la frustración, así como programas donde se trabaje la toma de decisiones, y se les enseñe a reflexionar sobre compartir ciertos contenidos online, el tipo de respuestas que dan ante ciertas situaciones y la asertividad.",
            "recomendation_mid": "Esta tasa de prevalencia es un indicador de riesgo importante para otras conductas delictivas, predelictivas y antisociales, así como para la violencia. Ya que las competencias de autocontrol que debieron ser adquiridas en la primera infancia y preadolescencia no fueron aprendidas lo que abre la puerta a una mala toma de decisiones y poca capacidad para la resolución de conflictos; de ahí que sea importante reforzar los valores de la comunidad, teniendo normas y consecuencias muy claras de las que la juventud esté informada a través de campañas de concientización. Así como es recomendable desarrollar talleres de comunicación y autocontrol con adolescentes y manejo de la frustración, así como programas donde se trabaje la toma de decisiones, y se les enseñe a reflexionar sobre compartir ciertos contenidos online, el tipo de respuestas que dan ante ciertas situaciones y la asertividad.",
            "recomendation_high": "Una tasa de prevalencia tan alta es un indicador de que más de la tercera parte de la población tiene dificultades para el autocontrol, lo que aumenta la probabilidad de que se caiga en otras conductas de riesgo como puede ser el consumo de estupefacientes, el uso de la violencia, los daños contra la propiedad y la normalización de todas ellas. Ya que las competencias de autocontrol que debieron ser adquiridas en la primera infancia y preadolescencia no fueron aprendidas. Es muy probable que con esta tasa de prevalencia existan grupos con problemas de consumo de estupefacientes (drogas blandas y/o duras) y que tengan conductas violentas, de ahí que sea importante reforzar los valores de la comunidad, teniendo normas y consecuencias muy claras de las que la juventud esté informada a través de campañas de concientización. Así como es recomendable desarrollar talleres de comunicación y autocontrol con adolescentes y manejo de la frustración, así como programas donde se trabaje la toma de decisiones, y se les enseñe a reflexionar sobre compartir ciertos contenidos online, el tipo de respuestas que dan ante ciertas situaciones y la asertividad.",
        },
        {
            "name": "Consumo de estupefacientes (Drogas duras)",
            "question": [
                {"id": 69, "answer": ["A"]},
                {"id": 70, "answer": ["A"]},
                {"id": 71, "answer": ["A"]},
                {"id": 72, "answer": ["A"]},
                {"id": 73, "answer": ["A"]},
                {"id": 74, "answer": ["A", "E"]},
                {"id": 75, "answer": ["A"]},
                {"id": 76, "answer": ["A"]},
                {"id": 77, "answer": ["A", "E"]},
                {"id": 78, "answer": ["B", "C"]},
                {"id": 79, "answer": ["B", "C"]},
            ],
            "prevalence_min": [5, 11],
            "prevalence_mid": [11, 21],
            "prevalence_high": [21, 101],
            "recomendation_min": "Esta tasa de prevalencia, es para poner en alerta a la comunidad, ya que se combina con las conductas temerarias propias de este rango de edad. Por lo que es recomendable desarrollar programas de prevención a través de charlas de impacto. Al igual que es importante generar espacios donde los adolescentes puedan hablar abiertamente de este tema. Por lo que se vuelve indispensable formar a los profesores en el tema e impartir cursos para los adultos en general pero sobre todo para los padres, de tal manera que sepan identificar los signos de consumo en la juventud y sepan a dónde pueden recurrir para solicitar ayuda.",
            "recomendation_mid": "Esta tasa de prevalencia es un indicador de que existen pequeños grupos habituales de consumo y que existe también cierta permisividad ante este comportamiento. Por lo que se recomienda desarrollar programas de prevención a través de charlas de impacto. Al igual que es importante generar espacios donde los adolescentes puedan hablar abiertamente de este tema. Por lo que se vuelve indispensable formar a los profesores en el tema e impartir cursos para los adultos en general, pero sobre todo para los padres. De tal manera que se promueva una cultura de legalidad dentro de la comunidad y se trabaje en normativas claras con sus consecuencias tanto dentro como fuera de los planteles ante este comportamiento para reforzar dicha cultura en los adolescentes.",
            "recomendation_high": "Esta tasa de prevalencia es un factor de riesgo muy alto por las conductas temerarias que son propias de esta edad. Expone el acceso a las drogas duras y la alta permisividad en el entorno inmediato. Así como a algunos grupos que ya presentan el consumo como un hábito, lo que puede significar que muy posiblemente también participen en otro tipo de conductas de riesgo como el consumo de alcohol, violencia física y violaciones contra la propiedad. Es recomendable desarrollar programas de prevención a través de charlas de impacto. Al igual que es importante generar espacios donde los adolescentes puedan hablar abiertamente de este tema. Por lo que se vuelve indispensable formar a los profesores en el tema e impartir cursos para los adultos en general, pero sobre todo para los padres. De tal manera que se promueva una cultura de legalidad dentro de la comunidad y se trabaje en normativas claras con sus consecuencias tanto dentro como fuera de los planteles ante este comportamiento para reforzar dicha cultura en los adolescentes y evitar la emulación de estas conductas por parte de la población más joven de la comunidad.",
        },
        {
            "name": "Violencia",
            "question": [
                {"id": 51, "answer": ["A"]},
                {"id": 52, "answer": ["A", "D", "E"]},
                {"id": 53, "answer": ["A", "E"]},
                {"id": 54, "answer": ["A"]},
                {"id": 55, "answer": ["A", "D", "E"]},
                {"id": 56, "answer": ["A", "D"]},
                {"id": 57, "answer": ["A"]},
                {"id": 50, "answer": ["A"]},
                {"id": 58, "answer": ["A"]},
                {"id": 59, "answer": ["A", "D", "E"]},
                {"id": 60, "answer": ["B", "C"]},
            ],
            "prevalence_min": [5, 16],
            "prevalence_mid": [16, 26],
            "prevalence_high": [26, 101],
            "recomendation_min": "Al ser un rango de edad en el que se consolidan la mayor parte de las conductas predelictivas y delictivas, es importante, que, con esta tasa de prevalencia se desarrollen programas sobre manejo de la frustración, resolución de conflictos, colaboración y sobre todo que se abran espacios de diálogo donde esta parte de la población se sienta con libertad de expresar sus necesidades y carencias tanto emocionales como sociales. Ya que a esta edad aparece la disconformidad y tienden a expresarla de la manera que atraiga la atención adulta, por lo que es indispensable que a la comunidad adulta se le entrene en habilidades de comunicación y mediación para reactivar los lazos de la población joven con su comunidad sin necesidad de ejercer la violencia para ser escuchados, obtener algo y/o resolver sus conflictos.",
            "recomendation_mid": "Al ser muy alta esta tasa de prevalencia en una edad en la que se consolidan la mayor parte de conductas de riesgo se puede caer en la indiferencia, por parte de la comunidad. Se hace necesario tener una normativa y consecuencias claras que vayan dirigidas a un aprendizaje de habilidades sociales para este tipo de comportamientos, es indispensable que dicha se centre en reactivar los lazos de la población joven con su comunidad sin necesidad de ejercer la violencia para ser escuchados, obtener algo y/o resolver sus conflictos. Para evitar la estigmatización de la juventud y lograr incorporarles a la comunidad a través de consecuencias prosociales que interioricen como un hábito.",
            "recomendation_high": "Esta prevalencia indica que ya está establecida la violencia como medio de resolución de conflictos en un número importante de la juventud. Indica la normalización de esta conducta también por parte de la comunidad y su posible indiferencia ante la presencia de esta, por lo que se hace indispensable realizar una intervención en todos los niveles, desarrollando programas de no tolerancia a la violencia donde se proporcionen herramientas para el manejo de la frustración, habilidades sociales y donde se eduque en la efectividad de la denuncia de la conducta de violencia física ejercida sobre alguien. Para poder disminuir los índices de violencia física entre los jóvenes, tiene que generarse un compromiso social a través de un entrenamiento que consiste en un cambio de esquemas y valores, premiando o reconociendo no solamente a las personas que no ejercen la violencia, sino a las que se atreven a denunciarla cuando la presencian. De igual forma Se hace necesario tener una normativa y consecuencias claras que vayan dirigidas a un aprendizaje de habilidades sociales para este tipo de comportamientos, es indispensable que dicha se centre en reactivar los lazos de la población joven con su comunidad sin necesidad de ejercer la violencia para ser escuchados, obtener algo y/o resolver sus conflictos. Para evitar la estigmatización de la juventud y lograr incorporarles a la comunidad a través de consecuencias prosociales que interioricen como un hábito. Evitando así que esta conducta vaya unida a otras de riesgo que haga que la violencia y la delincuencia juvenil se conviertan en conductas normalizadas y/o esperadas dentro de la población.",
        },
        {
            "name": "Acoso escolar",
            "question": [
                {"id": 20, "answer": ["A"]},
                {"id": 21, "answer": ["A"]},
                {"id": 22, "answer": ["A"]},
            ],
            "prevalence_min": [5, 16],
            "prevalence_mid": [16, 26],
            "prevalence_high": [26, 101],
            "recomendation_min": "Al ser un rango de edad en el que se consolidan la mayor parte de las conductas predelictivas y delictivas, es importante, que, con esta tasa de prevalencia se desarrollen programas sobre manejo de la frustración, resolución de conflictos, colaboración y sobre todo que se abran espacios de diálogo donde esta parte de la población se sienta con libertad de expresar sus necesidades y carencias tanto emocionales como sociales. Ya que a esta edad aparece la disconformidad y tienden a expresarla de la manera que atraiga la atención adulta, por lo que es indispensable que a la comunidad adulta se le entrene en habilidades de comunicación y mediación para reactivar los lazos de la población joven con su comunidad sin necesidad de ejercer la violencia para ser escuchados, obtener algo y/o resolver sus conflictos.",
            "recomendation_mid": "Al ser muy alta esta tasa de prevalencia en una edad en la que se consolidan la mayor parte de conductas de riesgo se puede caer en la indiferencia, por parte de la comunidad. Se hace necesario tener una normativa y consecuencias claras que vayan dirigidas a un aprendizaje de habilidades sociales para este tipo de comportamientos, es indispensable que dicha se centre en reactivar los lazos de la población joven con su comunidad sin necesidad de ejercer la violencia para ser escuchados, obtener algo y/o resolver sus conflictos. Para evitar la estigmatización de la juventud y lograr incorporarles a la comunidad a través de consecuencias prosociales que interioricen como un hábito.",
            "recomendation_high": "Esta prevalencia indica que ya está establecida la violencia como medio de resolución de conflictos en un número importante de la juventud. Indica la normalización de esta conducta también por parte de la comunidad y su posible indiferencia ante la presencia de esta, por lo que se hace indispensable realizar una intervención en todos los niveles, desarrollando programas de no tolerancia a la violencia donde se proporcionen herramientas para el manejo de la frustración, habilidades sociales y donde se eduque en la efectividad de la denuncia de la conducta de violencia física ejercida sobre alguien. Para poder disminuir los índices de violencia física entre los jóvenes, tiene que generarse un compromiso social a través de un entrenamiento que consiste en un cambio de esquemas y valores, premiando o reconociendo no solamente a las personas que no ejercen la violencia, sino a las que se atreven a denunciarla cuando la presencian. De igual forma Se hace necesario tener una normativa y consecuencias claras que vayan dirigidas a un aprendizaje de habilidades sociales para este tipo de comportamientos, es indispensable que dicha se centre en reactivar los lazos de la población joven con su comunidad sin necesidad de ejercer la violencia para ser escuchados, obtener algo y/o resolver sus conflictos. Para evitar la estigmatización de la juventud y lograr incorporarles a la comunidad a través de consecuencias prosociales que interioricen como un hábito. Evitando así que esta conducta vaya unida a otras de riesgo que haga que la violencia y la delincuencia juvenil se conviertan en conductas normalizadas y/o esperadas dentro de la población.",
        },
        {
            "name": "Conductas antisociales",
            "question": [
                {"id": 38, "answer": ["A", "C"]},
                {"id": 39, "answer": ["A", "D", "E"]},
                {"id": 40, "answer": ["A"]},
                {"id": 41, "answer": ["B"]},
                {"id": 42, "answer": ["A", "D", "E"]},
                {"id": 43, "answer": ["A", "C"]},
            ],
            "prevalence_min": [5, 16],
            "prevalence_mid": [16, 26],
            "prevalence_high": [26, 101],
            "recomendation_min": "Esta tasa de prevalencia en este rango de edad indica que existe uno o dos grupos pequeños que tienen este comportamiento interiorizado como estilo de vida, seguramente porque la evaluación del costo-beneficio de este comportamiento les compensa. Por lo que se recomienda que se desarrollen programas donde se fomenten los vínculos con la comunidad y el compromiso social. Haciéndoles partícipes de las decisiones que toma la institución educativa y/o el Ayuntamiento que implican a la juventud.",
            "recomendation_mid": "Esta tasa de prevalencia indica que la conducta está normalizada en este rango de edad y por lo tanto también existe cierta permisividad de parte de la comunidad. Es recomendable involucrar a los jóvenes en las decisiones que se tomen con relación a la juventud, tanto en la institución educativa, como en el Ayuntamiento, de tal manera que se sientan participes y esto genere un vínculo donde este tipo de comportamientos tienen un rechazo social. Iniciativas como la creación de un bono o carnet joven que le permita hacer determinada cantidad de viajes gratuitos, descuentos en libros, permitiendo que se afilien establecimientos de todo tipo para que perciban las ventajas de seguir las reglas y se habitúen a realizar algunos trámites para obtener dicho carnet. Así evaluaran que el costo de no cumplir las normas es más alto que el beneficio. Así como trabajar con ellos en la propuesta de normas y consecuencias en caso de que actúen de esta manera.",
            "recomendation_high": "Con una tasa de prevalencia que abarca más de la cuarta parte de la población joven se puede deducir que existen grupos que tienen este comportamiento como estilo de vida y que la comunidad es permisiva con este comportamiento. Se recomienda hacer partícipe a la juventud en las decisiones que tomen tanto la institución educativa como el Ayuntamiento con relación a este comportamiento. Así como realizar talleres que los vinculen más a la comunidad a la que pertenecen desarrollando programas sobre la toma de decisiones y la propuesta de normas y consecuencias hacia este comportamiento.",
        },
        {
            "name": "Relación alumnado - plantel",
            "question": [
                {"id": 3, "answer": ["A", "B"]},
                {"id": 18, "answer": ["A", "B", "C"]},
                {"id": 80, "answer": ["A"]},
                {"id": 82, "answer": ["A", "B"]},
                {"id": 83, "answer": ["A", "B"]},
                {"id": 84, "answer": ["A"]},
                {"id": 88, "answer": ["A"]},
                {"id": 89, "answer": ["A"]},
                {"id": 90, "answer": ["A"]},
                {"id": 91, "answer": ["B"]},
                {"id": 92, "answer": ["B"]},
                {"id": 93, "answer": ["A"]},
                {"id": 94, "answer": ["A"]},
                {"id": 119, "answer": ["A"]},
                {"id": 120, "answer": ["A"]},
            ],
            "prevalence_min": [25, 101],
            "recomendation_min": "Es alentador saber que más de la cuarta parte de la población del Bachiller tiene una buena relación con su plantel, reconocen el trabajo del personal docente y administrativo, confían en ellos y, además, se sienten valorados por ellos, ya que esto indica que el plantel está cumpliendo como agente de socialización. Lo que contribuye significativamente a la salud social del plantel y su entorno inmediato.",
        },
        {
            "name": "Familiares",
            "question": [
                {"id": 7, "answer": ["A", "B", "C"]},
                {"id": 8, "answer": ["D", "E"]},
                {"id": 9, "answer": ["D", "E"]},
                {"id": 10, "answer": ["B", "C"]},
                {"id": 11, "answer": ["B"]},
                {"id": 12, "answer": ["B"]},
                {"id": 116, "answer": ["B"]},
            ],
            "prevalence_min": [40, 101],
            "recomendation_min": "Cuando un cuarenta por ciento de  la población de más de 16 años en adelante  cuenta con una buena relación familiar, comunican con quién y a dónde van cuando salen de casas y/o cuentan con padres que han estudiado; es un indicador determinante a la hora de paliar con los factores de riesgo que existen dentro y fuera del plantel. Ya que es un factor protector vital que impacta directamente en el entorno inmediato debido a que este factor es el que determina el sistema de valores y creencias de los más jóvenes, así como su cultura de legalidad y la manera como se relacionarán con el entorno.",
        },
        {
            "name": "Personales",
            "question": [
                {"id": 13, "answer": ["B", "C", "D", "E"]},
                {"id": 19, "answer": ["A", "B"]},
                {"id": 85, "answer": ["A"]},
                {"id": 86, "answer": ["B"]},
                {"id": 118, "answer": ["A"]},
            ],
            "prevalence_min": [30, 101],
            "recomendation_min": "En la adolescencia es cuando más vulnerables se vuelven las personas, por lo que pasar la mayor parte del tiempo en compañía, considerar la importancia de estudiar, tener un buen auto-concepto con relación a los demás, así como sentirse bien con su género en una tercera parte de la población, indica que está presente un factor de protección determinante a la hora de lidiar con factores de riesgo como las adicciones o la violencia, ya que está demostrado que el auto-concepto, como el no sentirse en soledad, juegan un rol muy importante en la adolescencia y el consumo de sustancias, así como en la prevención de la ideación suicida.",
        },
    ],
    "ES": [
        {
            "name": "Ansiedad",
            "question": [
                {"id": 17, "answer": ["A"]},
                {"id": 24, "answer": ["A", "C"]},
                {"id": 25, "answer": ["A", "C"]},
                {"id": 26, "answer": ["A", "B"]},
                {"id": 27, "answer": ["A", "C"]},
                {"id": 95, "answer": ["A"]},
                {"id": 97, "answer": ["A"]},
                {"id": 98, "answer": ["A"]},
                {"id": 99, "answer": ["A"]},
                {"id": 100, "answer": ["A"]},
                {"id": 101, "answer": ["A"]},
                {"id": 102, "answer": ["A"]},
                {"id": 107, "answer": ["A"]},
                {"id": 114, "answer": ["A"]},
            ],
            "prevalence_min": [5, 11],
            "prevalence_mid": [11, 26],
            "prevalence_high": [26, 101],
            "recomendation_min": "Después de la pandemia es una tasa esperada, tomando en cuenta la edad y la incertidumbre que viven con relación al futuro, aunque no es muy alta es significativa y requiere de campañas de prevención de la ansiedad a nivel general.",
            "recomendation_mid": "Esta tasa ya es preocupante porque nos habla de una población joven con niveles de ansiedad altos, por lo que se recomienda que además de las campañas de prevención, haya intervenciones más directas, talleres de manejo de la ansiedad, información sobre identificación de síntomas y a quién recurrir.",
            "recomendation_high": "Se recomienda tener en el plantel y/o municipio, servicios específicos de ayuda en el manejo de la ansiedad, así como grupos terapéuticos. Ya que la tasa a partir de esta cifra es muy alta y se considera un agravante o detonante para la presencia de otros factores de riesgo.",
        },
        {
            "name": "Anhedonia",
            "question": [
                {"id": 4, "answer": ["A"]},
                {"id": 81, "answer": ["D"]},
                {"id": 82, "answer": ["D"]},
                {"id": 83, "answer": ["D"]},
                {"id": 85, "answer": ["A"]},
                {"id": 105, "answer": ["A"]},
                {"id": 106, "answer": ["A"]},
                {"id": 107, "answer": ["A"]},
                {"id": 108, "answer": ["A"]},
            ],
            "prevalence_min": [5, 11],
            "prevalence_mid": [11, 26],
            "prevalence_high": [26, 101],
            "recomendation_min": "Se recomienda tener en el plantel y/o municipio, servicios específicos de ayuda en el manejo de la ansiedad, así como grupos terapéuticos. Ya que la tasa a partir de esta cifra es muy alta y se considera un agravante o detonante para la presencia de otros factores de riesgo.",
            "recomendation_mid": "Conviene realizar campañas sobre la anhedonia, ya que una de las secuelas de la pandemia ha sido la presencia de síntomas anhedónicos en adolescentes y jóvenes, es importante trabajar junto con campañas de actividades académicas, deportivas y culturales que despierten la curiosidad, así como con campañas que hablen sobre las opciones laborales o de continuidad de estudios y becas.  También es indispensable una intervención más directa para explorar las relaciones familiares y con los pares del alumnado que presentan esta tasa.",
            "recomendation_high": "Con esta tasa la intervención directa en el alumnado a través de talleres o dinámicas grupales debe ser la prioridad, así como las campañas de prevención donde se les eduque con relación a los síntomas anhedónicos y los recursos de ayuda con los que cuenta el alumnado; ya que la manifestación de la anhedonia es un agravante o detonante de otras conductas de riesgo que pueden llevar al suicidio o al consumo de sustancias.",
        },
        {
            "name": "Depresión",
            "question": [
                {"id": 13, "answer": ["A"]},
                {"id": 19, "answer": ["C"]},
                {"id": 24, "answer": ["A", "C"]},
                {"id": 25, "answer": ["A", "C"]},
                {"id": 26, "answer": ["A"]},
                {"id": 90, "answer": ["B"]},
                {"id": 96, "answer": ["A"]},
                {"id": 103, "answer": ["A"]},
                {"id": 104, "answer": ["A"]},
                {"id": 105, "answer": ["A"]},
                {"id": 108, "answer": ["A"]},
                {"id": 109, "answer": ["A"]},
                {"id": 112, "answer": ["A"]},
                {"id": 113, "answer": ["A"]},
                {"id": 114, "answer": ["A"]},
                {"id": 115, "answer": ["A"]},
            ],
            "prevalence_min": [5, 11],
            "prevalence_mid": [11, 26],
            "prevalence_high": [26, 101],
            "recomendation_min": "Se recomienda tener en el plantel y/o municipio, servicios específicos de ayuda en el manejo de la ansiedad, así como grupos terapéuticos. Ya que la tasa a partir de esta cifra es muy alta y se considera un agravante o detonante para la presencia de otros factores de riesgo.",
            "recomendation_mid": "Conviene realizar campañas sobre la depresión, ya que una de las secuelas de la pandemia ha sido la presencia de síntomas depresivos en adolescentes y jóvenes, es importante trabajar junto con campañas de actividades académicas, deportivas y culturales que despierten la curiosidad, así como con campañas que hablen sobre las opciones laborales o de continuidad de estudios y becas.  También es indispensable una intervención más directa para explorar las relaciones familiares y con los pares del alumnado que presentan esta tasa.",
            "recomendation_high": "Con esta tasa la intervención directa en el alumnado a través de talleres o dinámicas grupales debe ser la prioridad, así como las campañas de prevención donde se les eduque con relación a los síntomas de la depresión y los recursos de ayuda con los que cuenta el alumnado; ya que la manifestación de la depresión es un agravante o detonante de otras conductas de riesgo que pueden llevar al suicidio o al consumo de sustancias.",
        },
        {
            "name": "Vandalismo",
            "question": [
                {"id": 44, "answer": ["A"]},
                {"id": 45, "answer": ["A", "D", "E"]},
                {"id": 46, "answer": ["A", "D", "E"]},
            ],
            "prevalence_min": [10, 16],
            "prevalence_mid": [16, 26],
            "prevalence_high": [26, 101],
            "recomendation_min": "La prevención de esta conducta en personas mayores de 20 años, no genera un efecto de persuasión, porque muy probablemente es un comportamiento que ya sea parte de su forma de socializar, por lo que hay que ir directamente a la intervención. Aplicando sin excepciones la normativa que se tenga, o realizar una nueva que se adapte a las características de la población y genere vínculos con su comunidad, de tal manera que les interese o les compense más tener comportamientos prosociales.",
            "recomendation_mid": "Cuanto más alto sea el porcentaje, más normalizada estará esta conducta, por lo que hay que realizar una intervención directa con las personas a las que se les descubra realizando esta conducta, aplicando la normativa o generando nuevas normativas que se adapten a las necesidades de la comunidad. Es imperante realizar talleres o actividades comunitarias para que la manera en la que interactúan con su comunidad sea más prosocial.",
            "recomendation_high": "Un porcentaje tan alto en este tipo de conductas predelictivas, habla de grupos (pandillas, bandas) que han instituido esta forma de interactuar con el resto de la comunidad y entre ellos, por lo que se sugiere que más que la normativa, se trabaje con talleres y actividades comunitarias para transformar su manera de interactuar con el resto de la comunidad.",
        },
        {
            "name": "Infracciones contra la propiedad",
            "question": [
                {"id": 47, "answer": ["A"]},
                {"id": 48, "answer": ["A", "D", "E"]},
                {"id": 49, "answer": ["A", "D", "E"]},
            ],
            "prevalence_min": [5, 16],
            "prevalence_mid": [16, 26],
            "prevalence_high": [26, 101],
            "recomendation_min": "A partir de los 20 años es muy probable que ya empiece a definirse una carrera delictiva, lo importante con este porcentaje que es significativo, pero no demasiado alto, además de aplicar consecuencias por cometer actos delictivos, será intervenir de manera directa en la población y promover talleres donde la misma población trabaje y haga conciencia de su cultura de legalidad y no permita la normalización de conductas delictivas a través de la resignación. Por lo que es importante reinventar la relación de las y los adultos jóvenes con su comunidad proporcionando espacios y herramientas que renueven la confianza y la participación de esta parte de la población. Población que puede generar conductas prosociales si en lugar de solamente castigarlos les proporcionamos alternativas para que no solamente se sientan productivos, sino que se conviertan en un recurso humano importante para la comunidad.",
            "recomendation_mid": "Esta tasa de prevalencia es un indicador de que ya existen grupos de adultos jóvenes que han asumido el robo como un estilo de vida y que la comunidad ha normalizado esta actividad casi con impunidad. Por lo que es imperante intervenir a nivel comunitario con talleres, charlas o dinámicas que fomenten la cultura de legalidad dentro de la población, evita la estigmatización y faciliten la intervención con la población joven, de tal manera que haya visibilidad sobre estas conductas y sobre todo de las consecuencias que reciben las conductas delictivas, sin centrarnos en el castigo, sino en el entrenamiento de las conductas prosociales que deberán recibir las personas que forman parte de esta tasa y así lograr que gran parte renueve su forma de interactuar socialmente a través de conductas prosociales.",
            "recomendation_high": "Siendo una tasa de prevalencia que supera la cuarta parte de las y los adultos jóvenes, la inhibición de esta conducta delictiva se convierte en prioridad, ya que esta tasa deja ver que además de existir grupos que tienen el robo como un estilo de vida dentro de su comunidad, también pone en evidencia que existe una alta impunidad, debido a la normalización que le da a esta conducta la población, resultado de una escasa cultura de legalidad. Por lo que además de tener y aplicar claramente unas normas y unas consecuencias para cualquiera que cometa este delito es necesario desarrollar programas que fomenten la cultura de la legalidad donde tanto la denuncia y la inhibición de esta conducta sean recompensadas por la comunidad. De esta manera, con el tiempo se podrán disminuir estas cifras en beneficio de la comunidad a la que pertenecen, evitando así la estigmatización y promoviendo el compromiso social. Mejorando así la salud social de toda la población.",
        },
        {
            "name": "Consumo de estupefacientes (Drogas blandas)",
            "question": [
                {"id": 61, "answer": ["A"]},
                {"id": 62, "answer": ["A"]},
                {"id": 63, "answer": ["A"]},
                {"id": 64, "answer": ["A"]},
                {"id": 65, "answer": ["A"]},
                {"id": 66, "answer": ["A"]},
                {"id": 67, "answer": ["A", "E"]},
                {"id": 68, "answer": ["B", "E"]},
            ],
            "prevalence_min": [5, 21],
            "prevalence_mid": [21, 36],
            "prevalence_high": [36, 101],
            "recomendation_min": "Esta tasa de prevalencia en realidad podría considerarse tolerable, tratándose de personas que son mayores de edad. Implica que existe un consumo moderado dentro de la población estudiantil en este rango de edad. Por lo que se recomienda que las campañas para inhibir la conducta, estén dirigidas más a las conductas de riesgo que se pueden detonar a partir del consumo de las drogas blandas. Por lo que las campañas deberían enfocarse a los efectos en la salud y la conciencia social sobre los efectos del consumo.",
            "recomendation_mid": "Esta tasa de prevalencia indica una normalización en el consumo de alcohol, tabaco y/o marihuana, pero también de permisividad de parte de la población adulta, lo que puede convertirse en un hábito y derivar en otras conductas de riesgo, por lo que es indispensable realizar campañas de concientización sobre el consumo y sus consecuencias. Así como se recomienda que se informe de la normativa que hay dentro de la Institución educativa y en el Ayuntamiento y sus posibles consecuencias al incumplirla. Es interesante trabajar con talleres donde sean los mismos jóvenes quienes propongan las consecuencias sociales hacia estos comportamientos o forma de minimizar los riesgos.",
            "recomendation_high": "Esta tasa de prevalencia es un indicador de que además de normalización y permisividad, existe un refuerzo social al consumo, es decir, que muy probablemente el consumo se de en presencia de adultos, ya sea en ambientes sociales o familiares y más que una represalia, se normalice o refuerce el consumo de alcohol, tabaco y/o marihuana de parte de los adultos presentes.. Se vuelve imperante desarrollar normativas claras con sus consecuencias legales y sociales ante el consumo masivo de drogas blandas. Así como se vuelve indispensable ofrecer formación a los adultos del contexto inmediato para formar un mismo frente y evitar que se convierta en la puerta hacia el consumo de las drogas duras, o hacia otras conductas de riesgo como la conducción temeraria y las relaciones sexuales sin protección.",
        },
        {
            "name": "Ideación suicida",
            "question": [
                {"id": 25, "answer": ["A", "C"]},
                {"id": 28, "answer": ["A", "B", "C"]},
                {"id": 110, "answer": ["A"]},
                {"id": 111, "answer": ["A"]},
                {"id": 112, "answer": ["A"]},
            ],
            "prevalence_min": [1, 6],
            "prevalence_mid": [6, 11],
            "prevalence_high": [11, 101],
            "recomendation_min": "Esta tasa de prevalencia es una señal importante de alerta. Siendo esta edad un factor de riesgo con relación al suicidio.  Donde se complica la intervención porque se tiene la idea de que los jóvenes no necesitan de un adulto que los acompañe, porque ya son adultos, pero son adultos jóvenes con mucha incertidumbre, agravada por la pandemia.  Es imperante que en las instituciones educativas se hable abiertamente del tema, sin tabúes, que se forme a todo el entorno escolar sobre los síntomas, y los recursos con los que cuenta.",
            "recomendation_mid": "Una tasa de prevalencia alta en este factor de riesgo es un indicador de que falta formación con relación al suicidio, y que hay una falta importante de recursos o de información sobre qué hacer si se tiene esta conducta o si conocemos a alguien que presenta ideación suicida. Donde se complica la intervención porque se tiene la idea de que los jóvenes no necesitan de un adulto que los acompañe, porque ya son adultos, pero son adultos jóvenes con mucha incertidumbre, agravada por la pandemia. Por lo que es imperante hablar abiertamente en el entorno escolar sobre el suicidio, los recursos, cómo servir de apoyo emocional en lo que se activan protocolos de seguridad, y sobre todo en la no banalización del tema cuando se trata de alumnado de este rango de edad, ya que la cifra se pudo haber visto aumentada como una consecuencia de la pandemia.",
            "recomendation_high": "Esta tasa implicaría un fracaso con relación al acompañamiento de la juventud. Donde se complica la intervención porque se tiene la idea de que los jóvenes no necesitan de un adulto que los acompañe, porque ya son adultos, pero son adultos jóvenes con mucha incertidumbre, agravada por la pandemia. Y se hace urgente hacer intervenciones directas y concientizar a toda la población con relación al suicidio. Implicaría que se hablara abiertamente del tema en todos los entornos, sin prejuicios, por el bien común, quitando el morbo y convirtiéndonos todos en agentes de apoyo emocional para los jóvenes de la comunidad, además de implementar líneas de ayuda y grupos de apoyo.",
        },
        {
            "name": "Adicción a las redes sociales",
            "question": [
                {"id": 32, "answer": ["B"]},
                {"id": 33, "answer": ["B"]},
                {"id": 35, "answer": ["B", "C"]},
                {"id": 36, "answer": ["C", "D", "E"]},
                {"id": 37, "answer": ["C", "D"]},
            ],
            "prevalence_min": [10, 26],
            "prevalence_mid": [26, 36],
            "prevalence_high": [36, 101],
            "recomendation_min": "Esta tasa implicaría un fracaso con relación al acompañamiento de la juventud. Donde se complica la intervención porque se tiene la idea de que los jóvenes no necesitan de un adulto que los acompañe, porque ya son adultos, pero son adultos jóvenes con mucha incertidumbre, agravada por la pandemia. Y se hace urgente hacer intervenciones directas y concientizar a toda la población con relación al suicidio. Implicaría que se hablara abiertamente del tema en todos los entornos, sin prejuicios, por el bien común, quitando el morbo y convirtiéndonos todos en agentes de apoyo emocional para los jóvenes de la comunidad, además de implementar líneas de ayuda y grupos de apoyo.",
            "recomendation_mid": "Esta tasa de prevalencia es esperable después de la pandemia, ya que gran parte de la socialización de los adultos jóvenes se tuvo que realizar a través de las redes sociales, lo que ha normalizado pasar muchas horas ante las redes sociales velando una posible adicción. Es recomendable desarrollar programas de manejo de adicciones a las redes sociales dentro de las Instituciones educativas ya que la adicción a las redes sociales no solamente afecta el rendimiento escolar, sino que tiene un impacto en la salud mental, afecta la autoestima, el auto-concepto y el sistema nervioso, ya que el tipo de luz interfiere en el ciclo circadiano, provocando problemas para dormir y ansiedad. Lo que repercute en otras conductas de riesgo. También se recomienda abrir espacios de ocio alternativo para que vuelvan a socializar sin depender de la tecnología y hacerlos participes de las campañas que se diseñen para combatir esta y otras adicciones detonadas por las redes sociales, como lo es la adicción a la pornografía.",
            "recomendation_high": "Al ser una tasa de prevalencia tan alta la mejor alternativa es hacer a los adultos jóvenes participes de las propuestas de ocio alternativo, dentro y fuera de la institución Educativa, que propongan campañas donde hablen de lo difícil que es soltar las redes sociales y cómo les afecta, tanto en el rendimiento escolar, como el impacto que tiene en la salud mental, cómo afecta su autoestima, su autoconcepto y el sistema nervioso, ya que el tipo de luz interfiere en el ciclo circadiano, provocando problemas para dormir y ansiedad. Lo que repercute en otras conductas de riesgo, como puede ser la depresión, ansiedad y otro tipo de conductas de riesgo que tienen que ver con la evasión. Se vuelve indispensable hacerlos participes en el diseño de las campañas para combatir esta y otras adicciones detonadas por las redes sociales, como lo es la adicción a la pornografía.",
        },
        {
            "name": "Control de impulsos",
            "question": [
                {"id": 23, "answer": ["A", "C"]},
            ],
            "prevalence_min": [10, 21],
            "prevalence_mid": [21, 31],
            "prevalence_high": [31, 101],
            "recomendation_min": "Esta tasa de prevalencia indica que existe una parte de la población adulta joven que podría estar desarrollando un trastorno de control de impulsos, trastorno que se caracteriza por la incapacidad de resistir un deseo, lo que suele significar un problema al momento de controlar las conductas agresivas, lo que hablaría de un grupo de personas que tienden a la violencia y/o al consumo de estupefacientes, ya que también es una incapacidad para resistir alguna tentación. Es importante reforzar los valores de la comunidad, teniendo normas y consecuencias muy claras esté y realizar campañas de concientización. Así como es recomendable desarrollar talleres de comunicación y autocontrol en la población adulta, así como el manejo de la frustración y, programas donde se trabaje la toma de decisiones, el tipo de respuestas que dan ante ciertas situaciones y la asertividad. Enseñándoles la importancia de tener la capacidad de aplazar y controlar los impulsos, aprendiendo a evaluar  costo - beneficio.",
            "recomendation_mid": "Esta tasa de prevalencia es alta, es recomendable desarrollar talleres de comunicación y autocontrol en la población adulta, así como el manejo de la frustración y, programas donde se trabaje la toma de decisiones, el tipo de respuestas que dan ante ciertas situaciones y la asertividad. Enseñándoles la importancia de tener la capacidad de aplazar y controlar los impulsos, aprendiendo a evaluar  costo - beneficio. También es un indicador de que puede haber un grupo importante de la población adulta joven que por esta falta de autocontrol sea más laxo con lo que a la cultura de legalidad se refiere, es decir, tienda a tener conductas predelictivas y/o violentas.",
            "recomendation_high": "Una tasa de prevalencia que abarca más de la tercera parte de la población con un pobre control de impulsos, va a dificultar que la población más joven desarrolle esa misma competencia, lo que nos daría una población mucho más laxa en su cultura de legalidad, más violenta y adictiva. Por lo que es recomendable que esforzar los valores de la comunidad, teniendo normas y consecuencias muy claras esté y realizar campañas de concientización. Así como es recomendable desarrollar talleres de comunicación y autocontrol en la población adulta, así como el manejo de la frustración y, programas donde se trabaje la toma de decisiones, el tipo de respuestas que dan ante ciertas situaciones y la asertividad. Enseñándoles la importancia de tener la capacidad de aplazar y controlar los impulsos, aprendiendo a evaluar costo – beneficio.",
        },
        {
            "name": "Consumo de estupefacientes (Drogas duras)",
            "question": [
                {"id": 69, "answer": ["A"]},
                {"id": 70, "answer": ["A"]},
                {"id": 71, "answer": ["A"]},
                {"id": 72, "answer": ["A"]},
                {"id": 73, "answer": ["A"]},
                {"id": 74, "answer": ["A", "E"]},
                {"id": 75, "answer": ["A"]},
                {"id": 76, "answer": ["A"]},
                {"id": 77, "answer": ["A", "E"]},
                {"id": 78, "answer": ["B", "C"]},
                {"id": 79, "answer": ["B", "C"]},
            ],
            "prevalence_min": [5, 11],
            "prevalence_mid": [11, 21],
            "prevalence_high": [21, 101],
            "recomendation_min": "Esta tasa de prevalencia en loa adultos jóvenes indica un hábito de consumo dentro de un pequeño grupo. Por lo que es indispensable formar a los profesores en el tema e impartir cursos para los adultos en general, pero sobre todo para los padres, para que así puedan identificar los signos de un consumo habitual y puedan ayudar y/o orientar a los adultos que empiezan a tener este hábito. También se recomienda informar sobre los recursos con los que cuentan dentro del municipio para solicitar apoyo en caso de que identifiquen estos signos en el alumnado, así como crear líneas telefónicas de ayuda.",
            "recomendation_mid": "Esta tasa de prevalencia es indicativa de que existen pequeños grupos habituales de consumo y que existe también cierta permisividad ante este comportamiento. Por lo que se recomienda desarrollar programas de prevención a través de charlas sobre las consecuencias penales y sociales de consumir drogas duras. Y se vuelve indispensable formar a los profesores en el tema e impartir cursos para los adultos en general, pero sobre todo para los padres. Desarrollar normativas claras con sus consecuencias tanto dentro como fuera de las instituciones educativas ante este comportamiento para reforzar la cultura de legalidad en los jóvenes adultos. También se recomienda informar sobre los recursos con los que cuentan dentro del municipio para solicitar apoyo en caso de que identifiquen estos signos en el alumnado, así como crear líneas telefónicas de ayuda. Previniendo así la emulación de estas conductas por parte de la población más joven de la comunidad.",
            "recomendation_high": "A partir de un 11% es una tasa de prevalencia que indica que existe cierta normalización del consumo de drogas duras en los adultos jóvenes y permisividad por parte de la comunidad. Por lo que se hace indispensable desarrollar programas de prevención a través de charlas sobre las consecuencias penales y sociales de consumir drogas duras. Y se recomienda formar a los profesores en el tema e impartir cursos para los adultos en general, pero sobre todo para los padres. Desarrollar normativas claras con sus consecuencias sociales y penales tanto dentro como fuera de las instituciones educativas ante este comportamiento para reforzar la cultura de legalidad en los jóvenes adultos. También se recomienda informar sobre los recursos con los que cuentan dentro del municipio para solicitar apoyo en caso de que identifiquen estos signos en el alumnado, como líneas telefónicas o informar en los centros de salud.",
        },
        {
            "name": "Violencia",
            "question": [
                {"id": 51, "answer": ["A"]},
                {"id": 52, "answer": ["A", "D", "E"]},
                {"id": 53, "answer": ["A", "E"]},
                {"id": 54, "answer": ["A"]},
                {"id": 55, "answer": ["A", "D", "E"]},
                {"id": 56, "answer": ["A", "D"]},
                {"id": 57, "answer": ["A"]},
                {"id": 50, "answer": ["A"]},
                {"id": 58, "answer": ["A"]},
                {"id": 59, "answer": ["A", "D", "E"]},
                {"id": 60, "answer": ["B", "C"]},
            ],
            "prevalence_min": [5, 16],
            "prevalence_mid": [16, 26],
            "prevalence_high": [26, 101],
            "recomendation_min": "A partir de esta edad los y las adultos jóvenes que presentan esta prevalencia tienen interiorizada la violencia física como un recurso al que recurrir siempre que algo no les agrade, quieran obtener algo o simplemente busquen divertirse. Moldear esta conducta a partir de esta edad se vuelve es más eficiente a través de consecuencias aversivas hacia este comportamiento, pero también con consecuencias positivas, como reconocimiento social a las personas que no la ejercen aun pudiendo hacerlo. Sobre todo, con una tasa de prevalencia que es significativa pero que no es alta.",
            "recomendation_mid": "Esta alta prevalencia es un indicador de que la conducta no solamente está normalizada dentro de la población, sino que también ha habido una indiferencia e impunidad con relación a esta conducta, ya que es una conducta que se va interiorizando desde la infancia como un mecanismo de interacción social, por lo que tuvieron que pasar la infancia, la adolescencia y ahora la adultez, con interacciones que han ido reforzando esta conducta de riesgo en vez de inhibirla. Por lo que es urgente valorar como actúa la población ante actos de violencia física y desarrollar programas que hagan conciencia del valor de la denuncia y la importancia de tener unas consecuencias claras para cada tipo de violencia física ejercida. Ya que con esta prevalencia se hace patente la presencia de grupos violentos que además pueden empezar a realizar otro tipo de conductas predelictivas y delictivas que aumenten la inseguridad en la comunidad.",
            "recomendation_high": "Cuando la prevalencia es demasiado alta en la violencia física, entonces estamos hablando de que existen grupos dentro de la población que han interiorizado esta conducta como un estilo de vida y con mucha impunidad por parte de la comunidad. Por lo que es imperante aplicar unas consecuencias firmes y claras con relación a este comportamiento, así como desarrollar programas de conciencia social con relación a esta conducta y compartir la responsabilidad de la impunidad e indiferencia con la que estos grupos crecen dentro de la comunidad. Para así lograr frenar su crecimiento y centrarnos en la reeducación social y evitar que se sumen más conductas de delictivas a este patrón, evitando zonas rojas dentro de una misma población.",
        },
        {
            "name": "Acoso escolar",
            "question": [
                {"id": 20, "answer": ["A"]},
                {"id": 21, "answer": ["A"]},
                {"id": 22, "answer": ["A"]},
            ],
            "prevalence_min": [10, 16],
            "prevalence_mid": [16, 26],
            "prevalence_high": [26, 101],
            "recomendation_min": "A partir de esta edad, ya estamos hablando de una conducta interiorizada y normalizada en los acosadores. Por lo que es indispensable que existan protocolos en los planteles y/o Ayuntamientos, normas claras con sus consecuencias sociales que manden el mensaje de que no hay tolerancia hacia este tipo de comportamientos. Es importante especificar el tipo de comportamientos que implica el acoso escolar ya que muchas veces está tan normalizado que no se dan cuenta de que lo que están haciendo es Acoso escolar. Por lo que la formación con relación a este tema es indispensable en todo el personal de la Institución Educativa.",
            "recomendation_mid": "A partir de esta edad, ya estamos hablando de una conducta interiorizada y normalizada en los acosadores. Lo que suele darse y perpetrarse por desconocimiento de aquellos comportamientos que implican acoso escolar pero que no se asumen como tal, y que puede abrir la puerta hacia una carrera delictiva si no se hacen campañas de información sobre las conductas que pueden considerarse de acoso escolar, tanto dentro como fuera de la institución educativa y sus implicaciones legales y sociales. Se recomienda que los padres participen en talleres de sensibilización, que existan grupos de apoyo a las víctimas de acoso escolar, así como grupos para trabajar con acosadores, de tal manera que se tenga una amplia cobertura del acoso escolar, logrando así inhibir la conducta y trabajando con los acosadores en formas más sanas de relacionarse con los demás. Convirtiendo a la población en agentes de prevención e intervención y evitando la indiferencia social.  Y previniendo posibles carreras delictivas dentro de los grupos que actúan de esta manera.",
            "recomendation_high": "Esta tasa de prevalencia en este rango de edad, es un indicador de la normalización de la conducta por parte de los acosadores que puede abrir las puertas a una carrera delictiva si no se toman medidas claras con relación a este tipo de comportamiento. Es imperante desarrollar programas donde participe la policía informando sobre el ciberacoso y los posibles delitos que se cometen al compartir información privada, que los padres participen en talleres de sensibilización, que existan grupos de apoyo a las víctimas de acoso escolar, así como grupos para trabajar con acosadores, de tal manera que se tenga una amplia cobertura del acoso escolar, logrando así inhibir la conducta y trabajando con los acosadores en formas más sanas de relacionarse con los demás. Convirtiendo a la población en agentes de prevención e intervención y evitando la indiferencia social.  Previniendo posibles carreras delictivas dentro de los grupos que actúan de esta manera.",
        },
        {
            "name": "Conductas antisociales",
            "question": [
                {"id": 38, "answer": ["A", "C"]},
                {"id": 39, "answer": ["A", "D", "E"]},
                {"id": 40, "answer": ["A"]},
                {"id": 41, "answer": ["B"]},
                {"id": 42, "answer": ["A", "D", "E"]},
                {"id": 43, "answer": ["A", "C"]},
            ],
            "prevalence_min": [10, 16],
            "prevalence_mid": [16, 26],
            "prevalence_high": [26, 101],
            "recomendation_min": "Esta tasa de prevalencia en adultos jóvenes, indica que existen pequeños grupos que han adoptado este comportamiento como estilo de vida. Por lo que es recomendable generar campañas de concientización social para que exista un rechazo social ante este comportamiento y se logre inhibir este comportamiento en los adultos jóvenes.",
            "recomendation_mid": "Esta tasa de prevalencia en los adultos jóvenes indica que este comportamiento está normalizado en la población, lo que implica un factor de riesgo también para la población más joven ya que indirectamente, al estar normalizado y existir cierta permisividad, se refuerza la conducta en la población más joven ya que emulan el comportamiento de los adultos, sobre todo de los más jóvenes. Por eso se recomienda que se tenga una normativa clara tanto dentro de la institución como en el Ayuntamiento con relación a este comportamiento.",
            "recomendation_high": "Con una tasa que abarca más de la cuarta parte de los adultos jóvenes, es importante cuestionarse la cultura de legalidad del contexto inmediato, ya que esto indica que existen grupos de personas que tienen este comportamiento como estilo de vida; lo que indicaría también, una posible carrera delictiva. Por eso se recomienda que se tenga una normativa clara tanto dentro de la institución como en el Ayuntamiento con relación a este comportamiento.",
        },
        {
            "name": "Relación alumnado - plantel",
            "question": [
                {"id": 3, "answer": ["A", "B"]},
                {"id": 18, "answer": ["A", "B", "C"]},
                {"id": 80, "answer": ["A"]},
                {"id": 82, "answer": ["A", "B"]},
                {"id": 83, "answer": ["A", "B"]},
                {"id": 84, "answer": ["A"]},
                {"id": 88, "answer": ["A"]},
                {"id": 89, "answer": ["A"]},
                {"id": 90, "answer": ["A"]},
                {"id": 91, "answer": ["B"]},
                {"id": 92, "answer": ["B"]},
                {"id": 93, "answer": ["A"]},
                {"id": 94, "answer": ["A"]},
                {"id": 119, "answer": ["A"]},
                {"id": 120, "answer": ["A"]},
            ],
            "prevalence_min": [15, 101],
            "recomendation_min": "Contar con más del diez por ciento de la población adulta joven que tiene una buena relación con su plantel es un indicador de que el plantel está cumpliendo como agente de socialización. Es muy buena noticia saber que los adultos jóvenes se sienten en su plantel con la confianza de moverse libremente y de recurrir al personal docente y administrativo en caso de tener algún problema, indica una comunicación horizontal reforzada y fomentada por el profesorado y el personal docente y administrativo, además de una implicación de parte de la población adulta que convive con ellos todos los días. Lo que contribuye significativamente a la salud social del plantel y su entorno inmediato.",
        },
        {
            "name": "Familiares",
            "question": [
                {"id": 7, "answer": ["A", "B", "C"]},
                {"id": 8, "answer": ["D", "E"]},
                {"id": 9, "answer": ["D", "E"]},
                {"id": 10, "answer": ["B", "C"]},
                {"id": 11, "answer": ["B"]},
                {"id": 12, "answer": ["B"]},
                {"id": 116, "answer": ["B"]},
            ],
            "prevalence_min": [40, 101],
            "recomendation_min": "En los adultos jóvenes las relaciones familiares son importantes, aunque no determinantes para detonar conductas de riesgo. Contar con más de la cuarta parte de la población adulta joven que cuenta con una buena relación familiar, o que cuentan con padres que han estudiado, es un factor protector determinante a la hora de paliar con los factores de riesgo. Pero también como conducta moldeadora que refuerza ese tipo de relación familiar entre los más jóvenes.",
        },
        {
            "name": "Personales",
            "question": [
                {"id": 13, "answer": ["B", "C", "D", "E"]},
                {"id": 19, "answer": ["A", "B"]},
                {"id": 85, "answer": ["A"]},
                {"id": 86, "answer": ["B"]},
                {"id": 118, "answer": ["A"]},
            ],
            "prevalence_min": [30, 101],
            "recomendation_min": "A partir de esta edad, tener un buen auto-concepto con relación a los demás, así como sentirse bien con su género en una tercera parte de la población, indica que está presente un factor de protección determinante a la hora de lidiar y mantener conductas de riesgo como las adicciones o la violencia. Así como también cumple una función reforzadora al ser una población que es emulada por lo más jóvenes.",
        },
    ],
}
result_teach = {
    "DOC": [
        {
            "name": "Ansiedad",
            "question": [
                {"id": 18, "answer": ["A"]},
                {"id": 25, "answer": ["A", "C"]},
                {"id": 27, "answer": ["A", "B"]},
                {"id": 28, "answer": ["A", "C"]},
                {"id": 97, "answer": ["A"]},
                {"id": 98, "answer": ["A"]},
                {"id": 99, "answer": ["A"]},
                {"id": 100, "answer": ["A"]},
                {"id": 101, "answer": ["A"]},
                {"id": 102, "answer": ["A"]},
                {"id": 107, "answer": ["A"]},
                {"id": 114, "answer": ["A"]},
            ],
            "prevalence_min": [10, 16],
            "prevalence_mid": [16, 26],
            "prevalence_high": [26, 101],
            "recomendation_min": "Después de la pandemia es una tasa esperada y significativa que requiere de campañas de prevención de la ansiedad a nivel general. Así como es importante crear grupos de apoyo donde se hable sobre el burnout que es uno de los males que asecha a los profesores sobre todo después de la pandemia y que se ve agravado por la presencia de síntomas de ansiedad.",
            "recomendation_mid": "Esta tasa ya es preocupante por lo que se recomienda que además de las campañas de prevención, haya intervenciones más directas, talleres de manejo de la ansiedad, información sobre identificación de síntomas y a quién recurrir. Es importante que se tenga información actualizada sobre cómo tratar la ansiedad e identificar las posibles crisis de ansiedad, así como los síntomas del burnout que pueden estar afectando al personal docente y/o administrativo.",
            "recomendation_high": "Se recomienda tener en el plantel y/o municipio, servicios específicos de ayuda en el manejo de la ansiedad, así como grupos terapéuticos. Ya que la tasa a partir de esta cifra es muy alta y se considera un agravante o detonante para la presencia de otros factores de riesgo como puede ser el burnout, la depresión, y el consumo de sustancias psicoactivas, así como para la violencia.",
        },
        {
            "name": "Anhedonia",
            "question": [
                {"id": 4, "answer": ["B"]},
                {"id": 81, "answer": ["D"]},
                {"id": 82, "answer": ["D"]},
                {"id": 83, "answer": ["D"]},
                {"id": 85, "answer": ["A"]},
                {"id": 105, "answer": ["A"]},
                {"id": 106, "answer": ["A"]},
                {"id": 107, "answer": ["A"]},
                {"id": 108, "answer": ["A"]},
            ],
            "prevalence_min": [5, 11],
            "prevalence_mid": [11, 26],
            "prevalence_high": [26, 101],
            "recomendation_min": "Es una tasa considerable donde conviene realizar campañas de actividades académicas, deportivas, culturales y laborales que despierten su curiosidad, como cursos de actualización, cursos de desarrollo personal o actividades que incluyan a todo el personal pero que sean recreativas.",
            "recomendation_mid": "Conviene realizar campañas sobre la anhedonia, ya que una de las secuelas de la pandemia ha sido la presencia de síntomas nhedónicos en toda la población, es importante trabajar junto con campañas de actividades académicas, deportivas y culturales que despierten la curiosidad, así como con campañas que hablen sobre las opciones de actualización laboral, desarrollo personal o cualquier otra actividad que pueda motivar a todo el personal.  También es indispensable una intervención más directa para explorar las relaciones familiares y la relación laboral del personal que presenta esta tasa.",
            "recomendation_high": "Conviene realizar campañas sobre la anhedonia, ya que una de las secuelas de la pandemia ha sido la presencia de síntomas nhedónicos en toda la población, es importante trabajar junto con campañas de actividades académicas, deportivas y culturales que despierten la curiosidad, así como con campañas que hablen sobre las opciones de actualización laboral, desarrollo personal o cualquier otra actividad que pueda motivar a todo el personal.  También es indispensable una intervención más directa para explorar las relaciones familiares y la relación laboral del personal que presenta esta tasa.",
        },
        {
            "name": "Depresión",
            "question": [
                {"id": 13, "answer": ["A"]},
                {"id": 19, "answer": ["C"]},
                {"id": 26, "answer": ["A", "C"]},
                {"id": 27, "answer": ["A"]},
                {"id": 28, "answer": ["A", "C"]},
                {"id": 90, "answer": ["B"]},
                {"id": 96, "answer": ["A"]},
                {"id": 97, "answer": ["A"]},
                {"id": 103, "answer": ["A"]},
                {"id": 104, "answer": ["A"]},
                {"id": 105, "answer": ["A"]},
                {"id": 108, "answer": ["A"]},
                {"id": 109, "answer": ["A"]},
                {"id": 110, "answer": ["A"]},
                {"id": 112, "answer": ["A"]},
                {"id": 113, "answer": ["A"]},
                {"id": 114, "answer": ["A"]},
                {"id": 115, "answer": ["A"]},
            ],
            "prevalence_min": [5, 11],
            "prevalence_mid": [11, 26],
            "prevalence_high": [26, 101],
            "recomendation_min": "Es una tasa considerable donde conviene realizar campañas de actividades académicas, deportivas y culturales y laborales que despierten su curiosidad, como cursos de actualización, cursos de desarrollo personal o actividades que incluyan a todo el personal pero que sean recreativas.",
            "recomendation_mid": "Conviene realizar campañas sobre la depresión, ya que una de las secuelas de la pandemia ha sido la presencia de síntomas depresivos en toda la población, es importante trabajar junto con campañas de actividades académicas, deportivas y culturales que despierten la curiosidad, así como con campañas que hablen sobre las opciones de actualización laboral, desarrollo personal o cualquier otra actividad que pueda motivar a todo el personal.  También es indispensable una intervención más directa para explorar las relaciones familiares y la relación laboral del personal que presenta esta tasa.",
            "recomendation_high": "Con esta tasa la intervención directa en el personal del plantel, es indispensable. Puede llevarse a través de talleres o dinámicas grupales, así como las campañas de prevención donde se les eduque con relación a los síntomas de la depresión y los recursos de ayuda con los que cuentan; ya que la manifestación de la depresión es un agravante o detonante de otras conductas de riesgo que pueden llevar al suicidio o al consumo de sustancias, así como al burnout.",
        },
        {
            "name": "Vandalismo",
            "question": [
                {"id": 45, "answer": ["A", "C"]},
                {"id": 46, "answer": ["A", "D", "E"]},
                {"id": 47, "answer": ["A", "D", "E"]},
            ],
            "prevalence_min": [5, 16],
            "prevalence_mid": [16, 26],
            "prevalence_high": [26, 101],
            "recomendation_min": "La prevención de esta conducta en personas mayores de 20 años, no genera un efecto de persuasión, porque muy probablemente es un comportamiento que ya sea parte de su forma de socializar, por lo que hay que ir directamente a la intervención aplicando, sin excepciones, la normativa que se tenga o realizar una nueva que se adapte a las características de la población y genere vínculos con su comunidad de tal manera que les interese o les compense más tener comportamientos prosociales.",
            "recomendation_mid": "Cuanto más alto sea el porcentaje, más normalizada estará esta conducta, por lo que hay que realizar una intervención directa con las personas a las que se les descubra realizando esta conducta, aplicando la normativa o generando nuevas normativas que se adapten a las necesidades de la comunidad. Es imperante realizar talleres o actividades comunitarias para que la manera en la que interactúan con su comunidad sea más prosocial. Sobre todo tratándose del personal docente y/o administrativo, ya que son los modelos a emular, por parte de la población más joven y el alumnado.",
            "recomendation_high": "Un porcentaje tan alto en este tipo de conductas predelictivas, habla de grupos (pandillas, bandas) que han instituido esta forma de interactuar con el resto de la comunidad y entre ellos, por lo que se sugiere que más que la normativa, se trabaje con talleres y actividades comunitarias para transformar su manera de interactuar con el resto de la comunidad. También sería de interés común aplicar o generar una normativa clara con sus consecuencias, cuando sea el personal docente y/o administrativo, quienes son sorprendidos actuando de esta manera dentro y fuera de la institución.",
        },
        {
            "name": "Infracciones contra la propiedad",
            "question": [
                {"id": 48, "answer": ["A"]},
                {"id": 49, "answer": ["A", "D", "E"]},
                {"id": 50, "answer": ["A", "D", "E"]},
            ],
            "prevalence_min": [5, 16],
            "prevalence_mid": [16, 26],
            "prevalence_high": [26, 101],
            "recomendation_min": "Este porcentaje que es significativo, sobre todo tratándose del personal docente y/o administrativo de una institución educativa pública. Por lo que es importante reinventar la relación del personal con su comunidad proporcionando espacios y herramientas que renueven la confianza y la participación de esta parte de la población. Población que puede generar conductas prosociales si en lugar de solamente castigarlos les proporcionamos alternativas para que no solamente se sientan productivos, sino que se conviertan en un recurso humano importante para la comunidad, además de la función que ya ejercen dentro de la institución.",
            "recomendation_mid": "Esta tasa de prevalencia, refleja que existen grupos dentro del personal docente y/o administrativo que han asumido el robo como un estilo de vida y que la comunidad ha normalizado esta actividad casi con impunidad. Por lo que es imperante intervenir a nivel comunitario con talleres, charlas o dinámicas que fomenten la cultura de legalidad dentro de la población. De tal manera que puedan convertirse en modelos a seguir para el alumnado.",
            "recomendation_high": "Siendo una tasa de prevalencia que supera la cuarta parte del personal docente y/o administrativo, la inhibición de esta conducta delictiva se convierte en prioridad, ya que esta tasa deja ver que además de existir grupos que tienen el robo como un estilo de vida dentro de su comunidad, también pone en evidencia que existe una alta impunidad, debido a la normalización que le da a esta conducta la población. Por lo que además de tener y aplicar claramente unas normas y unas consecuencias para cualquiera que cometa este delito es necesario desarrollar programas que fomenten la cultura de la legalidad donde tanto la denuncia y la inhibición de esta conducta sean recompensadas por la comunidad. De esta manera, con el tiempo se podrán disminuir estas cifras, promoviendo el compromiso social. Mejorando así la salud social de toda la población.",
        },
        {
            "name": "Consumo de estupefacientes (Drogas blandas)",
            "question": [
                {"id": 62, "answer": ["A"]},
                {"id": 63, "answer": ["A"]},
                {"id": 64, "answer": ["A"]},
                {"id": 65, "answer": ["A"]},
                {"id": 66, "answer": ["A"]},
                {"id": 67, "answer": ["A"]},
                {"id": 68, "answer": ["A", "E"]},
                {"id": 69, "answer": ["A", "E"]},
            ],
            "prevalence_min": [10, 26],
            "prevalence_mid": [26, 41],
            "prevalence_high": [41, 101],
            "recomendation_min": "Esta tasa de prevalencia indica que existe un grupo de personal docente y no docente que tienen como hábito el consumo de estupefacientes conocidos como drogas blandas, por lo que es necesario hacer campañas de concientización sobre lo que implica para la salud y el impacto social que tiene en el alumnado que el personal de la institución educativa sea un consumidor habitual.",
            "recomendation_mid": "Es recomendable desarrollar programas enfocados a informar y experimentar sobre las consecuencias del consumo de alcohol tanto a la población docente como a la población adulta en general. También es indispensable diseñar protocolos de acción dentro de los centros escolares y/o ayuntamientos, para lograr inhibir el consumo de alcohol, tabaco y marihuana. Así como ofrecer formación a los adultos del contexto inmediato para formar un mismo frente y evitar que se convierta en la puerta hacia el consumo de las drogas duras, o hacia otras conductas de riesgo como la conducción temeraria y las relaciones sexuales sin protección, o evitar que se conviertan en un modelo a seguir por el alumnado.",
            "recomendation_high": "Una tasa de prevalencia tan alta, indica normalización en el consumo de alcohol, tabaco y/o marihuana y permisividad por parte de la comunidad, por lo que seguramente comparten espacios de consumo sociales y/o familiares que sirven de reforzadores de esta conducta de riesgo. También es indispensable diseñar protocolos de acción dentro de las Instituciones Educativas y/o ayuntamientos, para lograr inhibir el consumo de alcohol, tabaco y marihuana. Así como se recomienda que se informe de la normativa que hay dentro de la Institución educativa y en el Ayuntamiento y sus posibles consecuencias al incumplirla. Sobre todo, porque el personal docente y no docente de una institución educativa son los modelos a seguir por parte del alumnado.",
        },
        {
            "name": "Ideación suicida",
            "question": [
                {"id": 26, "answer": ["A", "C"]},
                {"id": 29, "answer": ["A", "B", "C"]},
                {"id": 110, "answer": ["A"]},
                {"id": 111, "answer": ["A"]},
                {"id": 112, "answer": ["A"]},
            ],
            "prevalence_min": [1, 6],
            "prevalence_mid": [6, 16],
            "prevalence_high": [16, 101],
            "recomendation_min": "Es imperante que en las instituciones educativas se hable abiertamente del tema, sin tabúes, que se forme a todo el entorno escolar sobre los síntomas, y los recursos con los que cuenta, tanto dentro de la institución educativa, como fuera. Ya que cabe la creencia de que solamente hay que cuidar al alumnado, pero el personal docente y no docente son un engranaje vital para su funcionamiento, por lo que también su salud mental es importante. Así como proporcionarles los recursos necesarios para poder manejar una crisis de esta índole.",
            "recomendation_mid": "Una tasa de prevalencia alta en este factor de riesgo implica que falta formación con relación al suicidio, y que hay una falta importante de recursos o de información sobre qué hacer si se tiene esta conducta o si conocemos a alguien que presenta ideación suicida. Por lo que es imperante hablar abiertamente en el entorno escolar sobre el suicidio, los recursos, cómo servir d e apoyo emocional en lo que se activan protocolos de seguridad, y sobre todo en la no banalización del tema cuando se trata del personal docente y no docente dentro de la institución educativa, ya que la cifra se pudo haber visto aumentada como una consecuencia de la pandemia.",
            "recomendation_high": "Una tasa de prevalencia alta en este factor de riesgo implica que falta formación con relación al suicidio, y que hay una falta importante de recursos o de información sobre qué hacer si se tiene esta conducta o si conocemos a alguien que presenta ideación suicida. Por lo que es imperante hablar abiertamente en el entorno escolar sobre el suicidio, los recursos, cómo servir d e apoyo emocional en lo que se activan protocolos de seguridad, y sobre todo en la no banalización del tema cuando se trata del personal docente y no docente dentro de la institución educativa, ya que la cifra se pudo haber visto aumentada como una consecuencia de la pandemia.",
        },
        {
            "name": "Adicción a las redes sociales",
            "question": [
                {"id": 33, "answer": ["B"]},
                {"id": 34, "answer": ["B"]},
                {"id": 36, "answer": ["B", "C"]},
                {"id": 37, "answer": ["C", "D", "E"]},
                {"id": 38, "answer": ["C", "D"]},
            ],
            "prevalence_min": [10, 26],
            "prevalence_mid": [26, 36],
            "prevalence_high": [36, 101],
            "recomendation_min": "Inhibir una tasa de prevalencia significativa pero no alta en el uso de las redes sociales en adultos puede lograrse a través de la participación en la comunidad. Formar grupos de apoyo a esta adicción en sus términos y tomarlos en cuenta para las propuestas de un ocio alternativo les hace generar vínculos con la comunidad y reaprender formas de socialización con los adultos de su entorno inmediato. Así como una debida educación tecnológica.",
            "recomendation_mid": "Esta tasa de prevalencia es esperable después de la pandemia, ya que gran parte de la socialización se tuvo que realizar a través de las redes sociales, lo que ha normalizado pasar muchas horas ante las redes sociales velando una posible adicción. Es recomendable desarrollar programas de manejo de adicciones a las redes sociales dentro de las Instituciones educativa. También se recomienda abrir espacios de ocio alternativo para que vuelvan a socializar sin depender de la tecnología y hacerlos participes de las campañas que se diseñen para combatir esta y otras adicciones detonadas por las redes sociales, como lo es la adicción a la pornografía. Pensando siempre en que el alumnado emula a los adultos de su entorno inmediato, es indispensable educar en el uso de las tecnologías a los adultos de su contexto inmediato.",
            "recomendation_high": "Al ser una tasa de prevalencia tan alta la mejor alternativa es hacer al personal docente y no docente participes de las propuestas de ocio alternativo, dentro y fuera de la institución educativa, que propongan campañas donde hablen de lo difícil que es soltar las redes sociales y cómo les afecta, tanto en el rendimiento laboral, como el impacto que tiene en la familia y su salud mental. Lo que repercute en otras conductas de riesgo, como puede ser la depresión, ansiedad y otro tipo de conductas de riesgo que tienen que ver con la evasión. Se vuelve indispensable hacerlos participes en el diseño de las campañas para combatir esta y otras adicciones detonadas por las redes sociales, como lo es la adicción a la pornografía. Pensando siempre en que el alumnado emula a los adultos de su entorno inmediato, es indispensable educar en el uso de las tecnologías a los adultos de su contexto inmediato.",
        },
        {
            "name": "Control de impulsos",
            "question": [
                {"id": 24, "answer": ["A", "C"]},
            ],
            "prevalence_min": [10, 21],
            "prevalence_mid": [21, 31],
            "prevalence_high": [31, 101],
            "recomendation_min": "Esta tasa de prevalencia indica que existe una parte de la población docente y no docente de una institución educativa que podría estar desarrollando un trastorno de control de impulsos, trastorno que se caracteriza por la incapacidad de resistir un deseo, lo que suele significar un problema al momento de controlar las conductas agresivas, y que hablaría de un grupo de personas que tienden a la violencia y/o al consumo de estupefacientes, ya que también es una incapacidad para manejar adicciones. Es importante reforzar los valores de la comunidad, teniendo normas y consecuencias muy claras y realizar campañas de concientización. Enseñándoles la importancia de tener la capacidad de aplazar y controlar los impulsos, aprendiendo a evaluar  costo - beneficio.",
            "recomendation_mid": "Esta tasa de prevalencia es alta, lo que indica que a una gran parte de la población adulta de la institución educativa, le cuesta aplazar sus impulsos y deseos. Por lo que es importante reforzar los valores de la comunidad, teniendo normas y consecuencias muy claras y realizar campañas de concientización. Así como es recomendable desarrollar talleres de comunicación y autocontrol en la población adulta, como el manejo de la frustración y, programas donde se trabaje la toma de decisiones, el tipo de respuestas que dan ante ciertas situaciones y la asertividad. Enseñándoles la importancia de tener la capacidad de aplazar y controlar los impulsos, aprendiendo a evaluar costo - beneficio.",
            "recomendation_high": "Una tasa de prevalencia que abarca más de la tercera parte de la población con un pobre control de impulsos, va a dificultar que la población más joven desarrolle esa misma competencia, ya que los docentes y no docentes de una institución educativa, suelen ser modelos a seguir; lo que nos daría una población mucho más laxa en su cultura de legalidad, más violenta y adictiva. Por lo que es recomendable reforzar los valores de la comunidad, teniendo normas y consecuencias muy claras y realizar campañas de concientización. Así como es recomendable desarrollar talleres de comunicación y autocontrol en la población adulta, así como el manejo de la frustración y, programas donde se trabaje la toma de decisiones, el tipo de respuestas que dan ante ciertas situaciones y la asertividad. Enseñándoles la importancia de tener la capacidad de aplazar y controlar los impulsos, aprendiendo a evaluar  costo – beneficio.",
        },
        {
            "name": "Consumo de estupefacientes (Drogas duras)",
            "question": [
                {"id": 70, "answer": ["A"]},
                {"id": 71, "answer": ["A"]},
                {"id": 72, "answer": ["A"]},
                {"id": 73, "answer": ["A"]},
                {"id": 74, "answer": ["A"]},
                {"id": 75, "answer": ["A", "E"]},
                {"id": 76, "answer": ["A"]},
                {"id": 77, "answer": ["A"]},
                {"id": 78, "answer": ["A", "E"]},
                {"id": 79, "answer": ["B", "C"]},
            ],
            "prevalence_min": [5, 11],
            "prevalence_mid": [11, 21],
            "prevalence_high": [21, 101],
            "recomendation_min": "Esta tasa de prevalencia en los adultos indica un hábito de consumo dentro de un pequeño grupo. Por lo que es indispensable formar a los profesores en el tema e impartir cursos para los adultos en general, de tal manera que conozcan los síntomas de una persona que consume, así como de la abstinencia, pero sin estigmatizar, sino orientado a apoyar y a tocar estos temas sin tabúes. También se recomienda informar sobre los recursos con los que cuentan dentro del municipio para solicitar apoyo en caso de que identifiquen estos signos en el personal docente y no docente de una institución educativa, así como crear líneas telefónicas de ayuda.",
            "recomendation_mid": "Esta tasa de prevalencia es indicativa de que existen pequeños grupos habituales de consumo y que existe también cierta permisividad ante este comportamiento.. Desarrollar normativas claras con sus consecuencias tanto dentro como fuera de las instituciones educativas ante este comportamiento para reforzar la cultura de legalidad en el personal docente y no docente. También se recomienda informar sobre los recursos con los que cuentan dentro del municipio para solicitar apoyo en caso de que identifiquen estos signos en el personal, así como crear líneas telefónicas de ayuda. Previniendo así la emulación de estas conductas por parte del alumnado.",
            "recomendation_high": "Esta prevalencia es un indicador de que existe cierta normalización del consumo de drogas duras en los adultos de la institución educativa y permisividad por parte de la comunidad. Por lo que se hace indispensable desarrollar programas de prevención sobre las consecuencias penales y sociales de consumir drogas duras. Y se recomienda formar a los profesores en el tema. Desarrollar normativas claras con sus consecuencias sociales y penales tanto dentro como fuera de las instituciones educativas ante este comportamiento para reforzar la cultura de legalidad en los jóvenes adultos. También se recomienda informar sobre los recursos con los que cuentan dentro del municipio y del plantel para solicitar apoyo en caso de llegar a necesitarlo, previniendo así la emulación de estas conductas por parte del alumnado.",
        },
        {
            "name": "Violencia",
            "question": [
                {"id": 51, "answer": ["A"]},
                {"id": 52, "answer": ["A"]},
                {"id": 53, "answer": ["A", "D", "E"]},
                {"id": 54, "answer": ["A", "E"]},
                {"id": 55, "answer": ["A"]},
                {"id": 56, "answer": ["A", "D", "E"]},
                {"id": 57, "answer": ["A", "D"]},
                {"id": 58, "answer": ["A"]},
                {"id": 59, "answer": ["A"]},
                {"id": 60, "answer": ["B", "D", "E"]},
                {"id": 61, "answer": ["B", "C"]},
            ],
            "prevalence_min": [5, 16],
            "prevalence_mid": [16, 26],
            "prevalence_high": [26, 101],
            "recomendation_min": "El personal docente y administrativo que presentan esta prevalencia tienen interiorizada la violencia física. Moldear esta conducta en adultos se vuelve más eficiente a través de consecuencias aversivas hacia este comportamiento, pero también con consecuencias positivas, como reconocimiento social a las personas que no la ejercen aun pudiendo hacerlo. Sobre todo, con una tasa de prevalencia que es significativa pero que no es alta.",
            "recomendation_mid": "Esta alta prevalencia es un indicador de que la conducta no solamente está normalizada dentro de la población, sino que también indica que ha habido una indiferencia e impunidad con relación a esta conducta, ya que es una conducta que se va interiorizando desde la infancia como un mecanismo de interacción social, por lo que tuvieron que pasar la infancia, la adolescencia y ahora la adultez, con interacciones que han ido reforzando esta conducta de riesgo en vez de inhibirla; generando patrones de comportamiento y de resolución de conflictos que después emulará el alumnado de la institución educativa en la que trabajan. Por lo que es urgente valorar cómo actúa la población ante actos de violencia física y desarrollar programas que hagan conciencia del valor de la denuncia y la importancia de tener unas consecuencias claras para cada tipo de violencia ejercida.",
            "recomendation_high": "Cuando la prevalencia es demasiado alta en la violencia física, entonces estamos hablando de que existen grupos dentro del personal docente y administrativo que han interiorizado esta conducta como un medio de vida y con mucha impunidad por parte de la comunidad. Por lo que es imperante aplicar unas consecuencias firmes y claras con relación a este comportamiento, así como desarrollar programas de conciencia social con relación a esta conducta y compartir la responsabilidad de la impunidad e indiferencia con la que estos grupos crecen dentro de la comunidad. Para así lograr frenar su crecimiento y centrarnos en la reeducación social y evitar que se sumen más conductas de delictivas a este patrón, evitando zonas rojas dentro de una misma población.",
        },
        {
            "name": "Acoso escolar",
            "question": [
                {"id": 21, "answer": ["A"]},
                {"id": 22, "answer": ["A"]},
                {"id": 23, "answer": ["A"]},
            ],
            "prevalence_min": [5, 16],
            "prevalence_mid": [16, 26],
            "prevalence_high": [26, 101],
            "recomendation_min": "Cuando este comportamiento se manifiesta en el personal docente y no docente, se manifiesta que es una conducta interiorizada y normalizada en la institución educativa. Por lo que es indispensable que existan protocolos en los planteles y/o Ayuntamientos, normas claras con sus consecuencias sociales que manden el mensaje de que no hay tolerancia hacia este tipo de comportamientos, sobre todo tratándose de adultos que tienen como función ser un modelo social para el alumnado. Es importante especificar el tipo de comportamientos que implica el acoso escolar ya que muchas veces está tan normalizado que no se dan cuenta de que lo que están haciendo es Acoso escolar. Por lo que la formación con relación a este tema es indispensable en todo el personal de la Institución Educativa.",
            "recomendation_mid": "Cuando este comportamiento se manifiesta en el personal docente y no docente, se manifiesta que es una conducta interiorizada y normalizada en la institución educativa. Por lo que es indispensable que existan protocolos en los planteles y/o Ayuntamientos, normas claras con sus consecuencias sociales que manden el mensaje de que no hay tolerancia hacia este tipo de comportamientos, sobre todo tratándose de adultos que tienen como función ser un modelo social para el alumnado. Es importante especificar el tipo de comportamientos que implica el acoso escolar ya que muchas veces está tan normalizado que no se dan cuenta de que lo que están haciendo es Acoso escolar. Por lo que la formación con relación a este tema es indispensable en todo el personal de la Institución Educativa.",
            "recomendation_high": "La tasa de prevalencia tan alta de acoso escolar entre el personal docente y no docente es un indicador de la normalización de la conducta por parte de los acosadores que puede abrir las puertas a una carrera delictiva si no se toman medidas claras con relación a este tipo de comportamiento. Es imperante desarrollar programas donde participe la policía informando sobre el ciberacoso y los posibles delitos que se cometen al compartir información privada, que participen en talleres de sensibilización, que existan grupos de apoyo a las víctimas de acoso escolar, así como grupos para trabajar con acosadores, de tal manera que se tenga una amplia cobertura del acoso escolar, logrando así inhibir la conducta y trabajando con los acosadores en formas más sanas de relacionarse con los demás. Convirtiendo a la población en agentes de prevención e intervención y evitando la indiferencia social.  Generando nuevos modelos a seguir para el alumnado.",
        },
        {
            "name": "Conductas antisociales",
            "question": [
                {"id": 39, "answer": ["A", "C"]},
                {"id": 40, "answer": ["A", "D"]},
                {"id": 41, "answer": ["A"]},
                {"id": 42, "answer": ["A"]},
                {"id": 43, "answer": ["A", "D", "E"]},
                {"id": 44, "answer": ["A"]},
            ],
            "prevalence_min": [5, 16],
            "prevalence_mid": [16, 26],
            "prevalence_high": [26, 101],
            "recomendation_min": "En la edad adulta es muy probable que ya empiece a definirse una carrera delictiva dentro del personal docente y no docente, lo importante con este porcentaje que es significativo, pero no demasiado alto, además de aplicar consecuencias por cometer actos predelictivos, será intervenir de manera directa en la población y promover talleres donde la misma población trabaje y haga conciencia de su cultura de legalidad y no permita la normalización de conductas delictivas a través de la resignación.",
            "recomendation_mid": "Esta tasa de prevalencia, refleja que ya existen grupos dentro del personal docente y no docente que han asumido las conductas delictivas y predelictivas como un estilo de vida y que la comunidad ha normalizado esta actividad casi con impunidad. Por lo que es imperante intervenir a nivel comunitario con talleres, charlas o dinámicas que fomenten la cultura de legalidad dentro de la población, evite la estigmatización y faciliten la intervención con la población adulta, de tal manera que haya visibilidad sobre estas conductas y sobre todo de las consecuencias que reciben las conductas delictivas, sin centrarnos en el castigo, sino en el entrenamiento de las conductas prosociales que deberán recibir las personas que forman parte de esta tasa y así lograr que gran parte renueve su forma de interactuar socialmente a través de conductas prosociales.",
            "recomendation_high": "Siendo una tasa de prevalencia que supera la cuarta parte del personal docente y no docente de una institución educativa la inhibición de esta conducta delictiva se convierte en prioridad,, primero, porque se trata del personal de una institución educativa que sirven de modelos a seguir por parte del alumnado, y después porque esta tasa deja ver que además de existir grupos que tienen estas conductas como un estilo de vida dentro de su comunidad, también pone en evidencia que existe una alta impunidad, debido a la normalización que le da a esta conducta la población. Resultado de una escasa cultura de legalidad, generando un sentimiento general de inseguridad y de indiferencia antes estos actos, reforzando así el comportamiento antisocial como parte de la interacción social entre la población. Por lo que además de tener y aplicar claramente unas normas y unas consecuencias para cualquiera que cometa este delito es necesario desarrollar programas que fomenten la cultura de la legalidad donde tanto la denuncia y la inhibición de esta conducta sean recompensadas por la comunidad. De esta manera, con el tiempo se podrán disminuir estas cifras, aunque es necesario fomentar actividades y crear espacios de ocio, y productividad en la que los adultos puedan invertir su tiempo y ser productivos tanto para su propio beneficio como para el de la comunidad a la que pertenecen, evitando así la estigmatización y promoviendo el compromiso social. Mejorando así la salud social de toda la población.",
        },
        {
            "name": "Relación alumnado - plantel",
            "question": [
                {"id": 3, "answer": ["A", "B"]},
                {"id": 19, "answer": ["A", "B", "C"]},
                {"id": 81, "answer": ["A"]},
                {"id": 82, "answer": ["A", "B"]},
                {"id": 83, "answer": ["A", "B"]},
                {"id": 84, "answer": ["A"]},
                {"id": 88, "answer": ["A"]},
                {"id": 89, "answer": ["A"]},
                {"id": 90, "answer": ["A"]},
                {"id": 91, "answer": ["A"]},
                {"id": 92, "answer": ["A"]},
                {"id": 93, "answer": ["A"]},
                {"id": 94, "answer": ["A"]},
                {"id": 119, "answer": ["A"]},
                {"id": 120, "answer": ["A"]},
            ],
            "prevalence_min": [25, 101],
            "recomendation_min": "Con una cuarta parte de los docentes y no docentes de una institución educativa, que tienen una buena relación con su plantel, tienen sensación de seguridad, consideran que su plantel está limpio, sienten que sus alumnos valoran su trabajo y su entorno laboral también,  y además sienten que confían en ellos, esto se convierte en uno de los factores protectores más fuertes frente a las conductas de riesgo. Indica que el plantel está cumpliendo como agente de socialización. Es muy buena noticia saber que el personal docente y no docente de la institución educativa se sienten en su plantel con la confianza de moverse libremente y de recurrir a alguien del entorno laboral en caso de tener algún problema, indica una comunicación horizontal reforzada y fomentada por todos y un buen ambiente de trabajo, además de una implicación de parte de la población adulta que convive con ellos todos los días. Lo que contribuye significativamente a la salud social del plantel y su entorno inmediato.",
        },
        {
            "name": "Familiares",
            "question": [
                {"id": 7, "answer": ["A", "B", "C"]},
                {"id": 8, "answer": ["D", "E"]},
                {"id": 9, "answer": ["D", "E"]},
                {"id": 10, "answer": ["C", "D"]},
                {"id": 11, "answer": ["B"]},
                {"id": 12, "answer": ["B"]},
                {"id": 116, "answer": ["B"]},
            ],
            "prevalence_min": [30, 101],
            "recomendation_min": "La comunicación familiar, así como las relaciones familiares son un factor protector vital, que determina el sistema de valores y creencias de todas las personas, así como su cultura de legalidad y la manera como se relacionarán con el entorno. Por lo que al tener más de la tercera parte de la población más joven que cuenta con una buena relación familiar, o que cuentan con padres que han estudiado, las probabilidades de desarrollar hábitos basados en conductas de riesgo son menores, por lo que una buena relación familiar es un factor protector determinante a la hora de paliar con los factores de riesgo que existen dentro y fuera del plantel.",
        },
        {
            "name": "Personales",
            "question": [
                {"id": 13, "answer": ["B", "C", "D", "E"]},
                {"id": 19, "answer": ["A", "B"]},
                {"id": 85, "answer": ["B"]},
                {"id": 86, "answer": ["A"]},
                {"id": 118, "answer": ["A"]},
            ],
            "prevalence_min": [30, 101],
            "recomendation_min": "A partir de esta edad, tener un buen autoconcepto con relación a los demás, así como sentirse bien con su género en una cuarta parte de la población. Indica que está presente un factor de protección determinante a la hora de lidiar y mantener conductas de riesgo como las adicciones o la violencia. Así como también cumple una función reforzadora al ser una población que es emulada por lo más jóvenes.",
        },
    ]
}
ADMINS = [6751, 1]


class LoginView(View):
    def get(self, request, *args, **kwargs):
        if "logout" in kwargs:
            logout(request)
        form = LoginForm()
        msg = ""
        ctx = {}
        ctx["form"] = form
        ctx["msg"] = msg
        return render(request, "login.html", ctx)

    def post(self, request):
        msg = ""
        print(request.POST)
        form = LoginForm(request.POST)
        if form.is_valid():
            print(form.cleaned_data)
            us = form.cleaned_data["username"]
            pw = form.cleaned_data["password"]
            user = authenticate(username=us, password=pw)
            if user is not None and user.is_active:
                login(request, user)
                print("login succesful")
                if "redirect" in request.POST:
                    return HttpResponseRedirect(request.POST["redirect"])
                else:
                    return HttpResponseRedirect(reverse_lazy("intro"))
            else:
                messages.success(request, "Usuario y/o contraseña incorrecta")
        else:
            form = LoginForm()
            print(form.errors)
        ctx = {}
        ctx["form"] = form
        ctx["msg"] = msg
        return render(request, "login.html", ctx)


class IndexView(View):
    def get(self, request, *args, **kwargs):
        return render(request, "index.html", {})


def init_user(user):
    print(user.user.id)
    ctx = {}

    ctx["is_admin"] = False
    ctx["is_school"] = False
    ctx["is_school"] = False
    ctx["is_subsystem"] = False
    ctx["system"] = False
    ctx["is_muni"] = False
    ctx["is_state"] = False
    ctx["no_role"] = True
    try:
        if user.school:
            ctx["is_school"] = True
            ctx["school_emstype"] = user.school.get_emstype_display()
            ctx["school_cct"] = user.school.school_key
            ctx["school_name"] = user.school.school_name
            ctx["no_role"] = False
    except:
        pass
    try:
        if user.subsystem:
            ctx["is_subsystem"] = True
            ctx["subsystem_abrev"] = user.subsystem.abrev
            ctx["subsystem_name"] = user.subsystem.name
            ctx["no_role"] = False
    except:
        pass
    try:
        if user.level == 0:
            ctx["system"] = True
            ctx["system_acronym"] = "SEC"
            ctx["system_name"] = "Secundaria"
        elif user.level == 1:
            ctx["system"] = True
            ctx["system_acronym"] = "EMS"
            ctx["system_name"] = "Educación Media Superior"
        elif user.level == 2:
            ctx["system"] = True
            ctx["system_acronym"] = "ES"
            ctx["system_name"] = "Educación Superior"
        elif user.level == 3:
            ctx["is_muni"] = True
            ctx["muni_key"] = user.municipality.key
            ctx["muni_name"] = user.municipality.name
            ctx["no_role"] = False
        elif user.level == 4:
            ctx["is_state"] = True
            ctx["state_name"] = "Jalisco"
            ctx["no_role"] = False
        if user.level is not None and user.level in [0, 1, 2]:
            ctx["no_role"] = False
    except:
        pass
    if user.user.id in ADMINS:
        ctx["is_admin"] = True
    print(ctx)
    return ctx


class IntroView(View):
    def get(self, request, *args, **kwargs):
        ctx = init_user(request.user.userapp)
        ctx["location"] = "Introducción"
        ctx["location_name"] = "intro"
        return render(request, "dssh-intro.html", ctx)


class MapView(View):
    def get(self, request, *args, **kwargs):
        ctx = init_user(request.user.userapp)
        ctx["location"] = "Mapa"
        ctx["location_name"] = "map"
        ctx["map_levels"] = [
            {"value": level, "label": config["label"]}
            for level, config in MAP_LEVEL_CONFIG.items()
        ]
        ctx["map_subsystems"] = _resolve_map_subsystem("SEC")
        ctx["MAPBOX_TOKEN"] = settings.MAPBOX_TOKEN
        return render(request, "dssh-map.html", ctx)


class GlossaryView(View):
    def get(self, request, *args, **kwargs):
        ctx = init_user(request.user.userapp)
        ctx["location"] = "Glosario"
        ctx["location_name"] = "glossary"
        return render(request, "glosario.html", ctx)


def get_data(
    student_teacher=False,
    is_teacher=False,
    is_school=False,
    is_subsystem=False,
    is_system=False,
    is_muni=False,
    subsystem=None,
    system=None,
    system_name=None,
    school=None,
    muni=None,
    simple=False,
    level="SEC",
):
    def clamp_social_health(score):
        return max(0, 1000 - (77 * score))

    def has_negative_social_health(payload):
        if not isinstance(payload, dict):
            return False
        for key in ["social_health", "social_health_stud", "social_health_teach"]:
            value = payload.get(key)
            if isinstance(value, (int, float)) and value < 0:
                return True
        return False

    # search for same parameters
    init_level = level
    rresult = ReportResult.objects.filter(
        student_teacher=student_teacher,
        is_teacher=is_teacher,
        is_school=is_school,
        is_subsystem=is_subsystem,
        is_system=is_system,
        is_muni=is_muni,
        subsystem=subsystem,
        system=system,
        system_name=system_name,
        school=school,
        muni=muni,
        simple=simple,
        level=init_level,
    )
    if rresult.exists():
        ctx = rresult.last().result
        if not has_negative_social_health(ctx):
            return ctx
    ctx = {}
    print(muni)
    data = []
    data_teachers = []

    def get_count(query):
        _list = []
        if query:
            opts = query.first().question.get_all_options()
            for op in opts:
                freq = sum([answer.frequency for answer in query.filter(answer=op)])
                _list.append({"frequency": freq, "name": op})
            print(_list)
        return _list

    if is_school:
        social_health_score = 0
        if is_teacher or student_teacher:
            for level, factors in result_teach.items():
                for factor in factors:
                    total_sum = []
                    # print(factor["name"])
                    for q in factor["question"]:
                        # print(q)
                        if school.emstype == 0:
                            t = "M-SEC"
                        if school.emstype == 1:
                            t = "D-EMS"
                        if school.emstype == 2:
                            t = "D-ES"
                        key = f"{t}-{q['id']}"
                        question = Question.objects.filter(key=key).last()
                        # print(key)
                        # print(question)
                        answer = Answer.objects.filter(
                            school_id=school.id,
                            question_id=question.id,
                            answer__in=[question.get_option(qu) for qu in q["answer"]],
                        )
                        _sum = []
                        for a in answer:
                            if a.answer in question.get_all_options():
                                _sum.append(a.frequency)
                        res = sum(_sum)
                        q = Question.objects.filter(key__startswith=f"{t}").first()
                        teacher_count = sum(
                            [
                                a.frequency
                                for a in Answer.objects.filter(
                                    school_id=school.id, question_id=q.id
                                )
                            ]
                        )
                        try:
                            result = res / teacher_count
                        except Exception as e:
                            print(school.school_name, teacher_count)
                            # print(school.school_name,e)
                            result = 0
                        total_sum.append(round(result * 100, 2))
                    total_res = round((sum(total_sum) / len(total_sum)), 2)
                    # print(total_res)
                    if total_res > 100:
                        print(teacher_count)
                        print(res)
                        print(total_res)
                        total_res = 100
                    if total_res < factor["prevalence_min"][0]:
                        prevalence = 0
                        recomendation = "Enhorabuena, con esta conducta de riesgo tu plantel no tiene un problema."
                    elif (
                        total_res >= factor["prevalence_min"][0]
                        and total_res < factor["prevalence_min"][1]
                    ):
                        prevalence = 1
                        recomendation = factor["recomendation_min"]
                        if hasattr(factor, "prevalence_mid"):
                            social_health_score = social_health_score + 1
                    elif (
                        total_res >= factor["prevalence_mid"][0]
                        and total_res < factor["prevalence_mid"][1]
                    ):
                        prevalence = 2
                        recomendation = factor["recomendation_mid"]
                        social_health_score = social_health_score + 1
                    elif (
                        total_res >= factor["prevalence_high"][0]
                        and total_res < factor["prevalence_high"][1]
                    ):
                        prevalence = 3
                        recomendation = factor["recomendation_high"]
                        social_health_score = social_health_score + 1
                    if student_teacher:
                        data_teachers.append(
                            {
                                "factor": factor["name"],
                                "value": total_res,
                                "recomendation": recomendation if not simple else "",
                                "count": round(teacher_count * (total_res / 100)),
                                "prevalence": prevalence,
                            }
                        )
                    else:
                        data.append(
                            {
                                "factor": factor["name"],
                                "value": total_res,
                                "recomendation": recomendation if not simple else "",
                                "count": round(teacher_count * (total_res / 100)),
                                "prevalence": prevalence,
                            }
                        )
        if not is_teacher or student_teacher:
            for level, factors in result_dic.items():
                if level == "SEC" and school.emstype != 0:
                    continue
                if level == "EMS" and school.emstype != 1:
                    continue
                if level == "ES" and school.emstype != 2:
                    continue
                for factor in factors:
                    total_sum = []
                    # print(factor["name"])
                    for q in factor["question"]:
                        # print(q)
                        question = Question.objects.filter(
                            key=f"A-{level}-{q['id']}"
                        ).last()
                        answer = Answer.objects.filter(
                            school_id=school.id,
                            question_id=question.id,
                            answer__in=[question.get_option(qu) for qu in q["answer"]],
                        )
                        _sum = []
                        for a in answer:
                            # print(a.answer)
                            # print(a.frequency)
                            if a.answer in question.get_all_options():
                                _sum.append(a.frequency)
                        res = sum(_sum)
                        q = Question.objects.filter(
                            key__startswith=f"A-{level}"
                        ).first()
                        student_count = sum(
                            [
                                a.frequency
                                for a in Answer.objects.filter(
                                    school_id=school.id, question_id=q.id
                                )
                            ]
                        )
                        try:
                            result = res / student_count
                        except Exception as e:
                            print(school.school_name, e)
                            continue
                        total_sum.append(round(result * 100, 2))
                    # print(total_sum)
                    # print(sum(total_sum))
                    # print(len(total_sum))
                    try:
                        total_res = round((sum(total_sum) / len(total_sum)), 2)
                    except Exception as e:
                        continue
                    if total_res < factor["prevalence_min"][0]:
                        prevalence = 0
                        recomendation = "Enhorabuena, con esta conducta de riesgo tu plantel no tiene un problema."
                    elif (
                        total_res >= factor["prevalence_min"][0]
                        and total_res < factor["prevalence_min"][1]
                    ):
                        prevalence = 1
                        recomendation = factor["recomendation_min"]
                        if hasattr(factor, "prevalence_mid"):
                            social_health_score = social_health_score + 1
                    elif (
                        total_res >= factor["prevalence_mid"][0]
                        and total_res < factor["prevalence_mid"][1]
                    ):
                        prevalence = 2
                        recomendation = factor["recomendation_mid"]
                        social_health_score = social_health_score + 1
                    elif (
                        total_res >= factor["prevalence_high"][0]
                        and total_res < factor["prevalence_high"][1]
                    ):
                        prevalence = 3
                        recomendation = factor["recomendation_high"]
                        social_health_score = social_health_score + 1
                    elif (
                        total_res >= factor["prevalence_veryhigh"][0]
                        and total_res <= factor["prevalence_veryhigh"][1]
                    ):
                        prevalence = 4
                        recomendation = factor["recomendation_veryhigh"]
                        social_health_score = social_health_score + 1
                    data.append(
                        {
                            "factor": factor["name"],
                            "value": total_res,
                            "recomendation": recomendation if not simple else "",
                            "count": round(student_count * (total_res / 100)),
                            "prevalence": prevalence,
                        }
                    )
        ctx["is_school"] = True
        ctx["school_name"] = school.school_name
        ctx["school_emstype"] = (
            f"{school.get_emstype_display()} / {school.subsystem.abrev}"
        )
        ctx["school_key"] = school.school_key
        ctx["school_id"] = school.id
        ctx["muni_name"] = school.muni.name
        ctx["terminal_efficiency"] = round(school.terminal_efficiency, 2)
        ctx["reprobation"] = round(school.reprobation, 2)
        # print(social_health_score)
        ctx["social_health"] = clamp_social_health(social_health_score)
        if not simple:
            if school.emstype == 0:
                t = "M-SEC"
                s = "A-SEC"
            elif school.emstype == 1:
                t = "D-EMS"
                s = "A-EMS"
            elif school.emstype == 2:
                t = "D-ES"
                s = "A-ES"
            if is_teacher or student_teacher:
                age = Answer.objects.filter(school_id=school.id, question__key=f"{t}-1")
                gender = Answer.objects.filter(
                    school_id=school.id, question__key=f"{t}-2"
                )
                work = Answer.objects.filter(
                    school_id=school.id, question__key=f"{t}-5"
                )
                home = Answer.objects.filter(
                    school_id=school.id, question__key=f"{t}-6"
                )
                if student_teacher:
                    ctx["gender_data_teachers"] = [
                        {"frequency": answer.frequency, "name": answer.answer}
                        for answer in gender.order_by("question__key")
                    ]
                    ctx["age_data_teachers"] = [
                        {"frequency": answer.frequency, "name": answer.answer}
                        for answer in age.order_by("question__key")
                    ]
                    ctx["work_data_teachers"] = [
                        {"frequency": answer.frequency, "name": answer.answer}
                        for answer in work.order_by("question__key")
                    ]
                    ctx["home_data_teachers"] = [
                        {"frequency": answer.frequency, "name": answer.answer}
                        for answer in home.order_by("question__key")
                    ]
                else:
                    ctx["gender_data"] = [
                        {"frequency": answer.frequency, "name": answer.answer}
                        for answer in gender.order_by("question__key")
                    ]
                    ctx["age_data"] = [
                        {"frequency": answer.frequency, "name": answer.answer}
                        for answer in age.order_by("question__key")
                    ]
                    ctx["work_data"] = [
                        {"frequency": answer.frequency, "name": answer.answer}
                        for answer in work.order_by("question__key")
                    ]
                    ctx["home_data"] = [
                        {"frequency": answer.frequency, "name": answer.answer}
                        for answer in home.order_by("question__key")
                    ]
            if not is_teacher or student_teacher:
                age = Answer.objects.filter(school_id=school.id, question__key=f"{s}-1")
                gender = Answer.objects.filter(
                    school_id=school.id, question__key=f"{s}-2"
                )
                work = Answer.objects.filter(
                    school_id=school.id, question__key=f"{s}-5"
                )
                home = Answer.objects.filter(
                    school_id=school.id, question__key=f"{s}-6"
                )
                if student_teacher:
                    ctx["gender_data_student"] = [
                        {"frequency": answer.frequency, "name": answer.answer}
                        for answer in gender.order_by("question__key")
                    ]
                    ctx["age_data_student"] = [
                        {"frequency": answer.frequency, "name": answer.answer}
                        for answer in age.order_by("question__key")
                    ]
                    ctx["work_data_student"] = [
                        {"frequency": answer.frequency, "name": answer.answer}
                        for answer in work.order_by("question__key")
                    ]
                    ctx["home_data_student"] = [
                        {"frequency": answer.frequency, "name": answer.answer}
                        for answer in home.order_by("question__key")
                    ]
                else:
                    ctx["gender_data"] = [
                        {"frequency": answer.frequency, "name": answer.answer}
                        for answer in gender.order_by("question__key")
                    ]
                    ctx["age_data"] = [
                        {"frequency": answer.frequency, "name": answer.answer}
                        for answer in age.order_by("question__key")
                    ]
                    ctx["work_data"] = [
                        {"frequency": answer.frequency, "name": answer.answer}
                        for answer in work.order_by("question__key")
                    ]
                    ctx["home_data"] = [
                        {"frequency": answer.frequency, "name": answer.answer}
                        for answer in home.order_by("question__key")
                    ]

    elif is_subsystem:

        social_health_score = 0
        # print(kwargs)
        if is_teacher:
            for level, factors in result_teach.items():
                for factor in factors:
                    total_sum = []
                    # print(factor["name"])
                    for q in factor["question"]:
                        # print(q)
                        if school.emstype == 0:
                            t = "M-SEC"
                        if school.emstype == 1:
                            t = "D-EMS"
                        if school.emstype == 2:
                            t = "D-ES"
                        key = f"{t}-{q['id']}"
                        question = Question.objects.filter(key=key).last()
                        # print(key)
                        # print(question)
                        answer = Answer.objects.filter(
                            school__subsystem__id=subsystem.id,
                            question_id=question.id,
                            answer__in=[question.get_option(qu) for qu in q["answer"]],
                        )
                        _sum = []
                        for a in answer:
                            if a.answer in question.get_all_options():
                                _sum.append(a.frequency)
                        res = sum(_sum)
                        q = Question.objects.filter(key__startswith=f"{t}").first()
                        teacher_count = sum(
                            [
                                a.frequency
                                for a in Answer.objects.filter(
                                    school__subsystem__id=subsystem.id, question_id=q.id
                                )
                            ]
                        )
                        result = res / teacher_count
                        total_sum.append(round(result * 100, 2))
                    total_res = round((sum(total_sum) / len(total_sum)), 2)
                    # print(total_res)
                    if total_res < factor["prevalence_min"][0]:
                        prevalence = 0
                        recomendation = "Enhorabuena, con esta conducta de riesgo tu plantel no tiene un problema."
                    elif (
                        total_res >= factor["prevalence_min"][0]
                        and total_res < factor["prevalence_min"][1]
                    ):
                        prevalence = 1
                        recomendation = factor["recomendation_min"]
                        if hasattr(factor, "prevalence_mid"):
                            social_health_score = social_health_score + 1
                    elif (
                        total_res >= factor["prevalence_mid"][0]
                        and total_res < factor["prevalence_mid"][1]
                    ):
                        prevalence = 2
                        recomendation = factor["recomendation_mid"]
                        social_health_score = social_health_score + 1
                    elif (
                        total_res >= factor["prevalence_high"][0]
                        and total_res < factor["prevalence_high"][1]
                    ):
                        prevalence = 3
                        recomendation = factor["recomendation_high"]
                        social_health_score = social_health_score + 1
                    print(round(teacher_count * (total_res / 100)))
                    data.append(
                        {
                            "factor": factor["name"],
                            "value": total_res,
                            "recomendation": recomendation if not simple else "",
                            "count": round(teacher_count * (total_res / 100)),
                            "prevalence": prevalence,
                        }
                    )
        else:
            for level, factors in result_dic.items():
                if level == "SEC" and school.emstype != 0:
                    continue
                if level == "EMS" and school.emstype != 1:
                    continue
                if level == "ES" and school.emstype != 2:
                    continue
                for factor in factors:
                    total_sum = []
                    # print(factor["name"])
                    for q in factor["question"]:
                        # print(q)
                        question = Question.objects.filter(
                            key=f"A-{level}-{q['id']}"
                        ).last()
                        answer = Answer.objects.filter(
                            school__subsystem__id=subsystem.id,
                            question_id=question.id,
                            answer__in=[question.get_option(qu) for qu in q["answer"]],
                        )
                        _sum = []
                        for a in answer:
                            # print(a.answer)
                            # print(a.frequency)
                            if a.answer in question.get_all_options():
                                _sum.append(a.frequency)
                        res = sum(_sum)
                        q = Question.objects.filter(
                            key__startswith=f"A-{level}"
                        ).first()
                        student_count = sum(
                            [
                                a.frequency
                                for a in Answer.objects.filter(
                                    school__subsystem__id=subsystem.id, question_id=q.id
                                )
                            ]
                        )
                        result = res / student_count
                        total_sum.append(round(result * 100, 2))
                    # print(total_sum)
                    # print(sum(total_sum))
                    # print(len(total_sum))
                    total_res = round((sum(total_sum) / len(total_sum)), 2)
                    if total_res < factor["prevalence_min"][0]:
                        prevalence = 0
                        recomendation = "Enhorabuena, con esta conducta de riesgo tu plantel no tiene un problema."
                    elif (
                        total_res >= factor["prevalence_min"][0]
                        and total_res < factor["prevalence_min"][1]
                    ):
                        prevalence = 1
                        recomendation = factor["recomendation_min"]
                        if hasattr(factor, "prevalence_mid"):
                            social_health_score = social_health_score + 1
                    elif (
                        total_res >= factor["prevalence_mid"][0]
                        and total_res < factor["prevalence_mid"][1]
                    ):
                        prevalence = 2
                        recomendation = factor["recomendation_mid"]
                        social_health_score = social_health_score + 1
                    elif (
                        total_res >= factor["prevalence_high"][0]
                        and total_res < factor["prevalence_high"][1]
                    ):
                        prevalence = 3
                        recomendation = factor["recomendation_high"]
                        social_health_score = social_health_score + 1
                    elif (
                        total_res >= factor["prevalence_veryhigh"][0]
                        and total_res <= factor["prevalence_veryhigh"][1]
                    ):
                        prevalence = 4
                        recomendation = factor["recomendation_veryhigh"]
                        social_health_score = social_health_score + 1
                    data.append(
                        {
                            "factor": factor["name"],
                            "value": total_res,
                            "recomendation": recomendation if not simple else "",
                            "prevalence": prevalence,
                            "count": round(student_count * (total_res / 100)),
                        }
                    )
        ctx["subsystem_name"] = subsystem.name
        ctx["subsystem_abrev"] = subsystem.abrev
        ctx["subsystem"] = True

        # print(social_health_score)
        ctx["social_health"] = clamp_social_health(social_health_score)
        if not simple:
            if school.emstype == 0:
                t = "M-SEC" if is_teacher else "A-SEC"
                age = Answer.objects.filter(
                    school__subsystem__id=subsystem.id, question__key=f"{t}-1"
                )
                gender = Answer.objects.filter(
                    school__subsystem__id=subsystem.id, question__key=f"{t}-2"
                )
                work = Answer.objects.filter(
                    school__subsystem__id=subsystem.id, question__key=f"{t}-5"
                )
                home = Answer.objects.filter(
                    school__subsystem__id=subsystem.id, question__key=f"{t}-6"
                )
            elif school.emstype == 1:
                t = "D-EMS" if is_teacher else "A-EMS"
                age = Answer.objects.filter(
                    school__subsystem__id=subsystem.id, question__key=f"{t}-1"
                )
                gender = Answer.objects.filter(
                    school__subsystem__id=subsystem.id, question__key=f"{t}-2"
                )
                work = Answer.objects.filter(
                    school__subsystem__id=subsystem.id, question__key=f"{t}-5"
                )
                home = Answer.objects.filter(
                    school__subsystem__id=subsystem.id, question__key=f"{t}-6"
                )
            elif school.emstype == 2:
                t = "D-ES" if is_teacher else "A-ES"
                age = Answer.objects.filter(
                    school__subsystem__id=subsystem.id, question__key=f"{t}-1"
                )
                gender = Answer.objects.filter(
                    school__subsystem__id=subsystem.id, question__key=f"{t}-2"
                )
                work = Answer.objects.filter(
                    school__subsystem__id=subsystem.id, question__key=f"{t}-5"
                )
                home = Answer.objects.filter(
                    school__subsystem__id=subsystem.id, question__key=f"{t}-6"
                )
            # [question.get_option(qu) for qu in q['answer']]
            ctx["gender_data"] = get_count(gender)
            ctx["age_data"] = get_count(age)
            ctx["work_data"] = get_count(work)
            ctx["home_data"] = get_count(home)

    elif is_system:

        social_health_score = 0
        # print(kwargs)
        if is_teacher:
            for level, factors in result_teach.items():
                for factor in factors:
                    total_sum = []
                    # print(factor["name"])
                    for q in factor["question"]:
                        # print(q)
                        if system == 0:
                            t = "M-SEC"
                        if system == 1:
                            t = "D-EMS"
                        if system == 2:
                            t = "D-ES"
                        key = f"{t}-{q['id']}"
                        question = Question.objects.filter(key=key).last()
                        # print(key)
                        # print(question)
                        answer = Answer.objects.filter(
                            school__emstype=system,
                            question_id=question.id,
                            answer__in=[question.get_option(qu) for qu in q["answer"]],
                        )
                        _sum = []
                        for a in answer:
                            if a.answer in question.get_all_options():
                                _sum.append(a.frequency)
                        res = sum(_sum)
                        q = Question.objects.filter(key__startswith=f"{t}").first()
                        teacher_count = sum(
                            [
                                a.frequency
                                for a in Answer.objects.filter(
                                    school__emstype=system, question_id=q.id
                                )
                            ]
                        )
                        result = res / teacher_count
                        total_sum.append(round(result * 100, 2))
                    total_res = round((sum(total_sum) / len(total_sum)), 2)
                    # print(total_res)
                    if total_res < factor["prevalence_min"][0]:
                        prevalence = 0
                        recomendation = "Enhorabuena, con esta conducta de riesgo tu plantel no tiene un problema."
                    elif (
                        total_res >= factor["prevalence_min"][0]
                        and total_res < factor["prevalence_min"][1]
                    ):
                        prevalence = 1
                        recomendation = factor["recomendation_min"]
                        if hasattr(factor, "prevalence_mid"):
                            social_health_score = social_health_score + 1
                    elif (
                        total_res >= factor["prevalence_mid"][0]
                        and total_res < factor["prevalence_mid"][1]
                    ):
                        prevalence = 2
                        recomendation = factor["recomendation_mid"]
                        social_health_score = social_health_score + 1
                    elif (
                        total_res >= factor["prevalence_high"][0]
                        and total_res < factor["prevalence_high"][1]
                    ):
                        prevalence = 3
                        recomendation = factor["recomendation_high"]
                        social_health_score = social_health_score + 1
                    data.append(
                        {
                            "factor": factor["name"],
                            "value": total_res,
                            "recomendation": recomendation if not simple else "",
                            "count": round(teacher_count * (total_res / 100)),
                            "prevalence": prevalence,
                        }
                    )
        else:
            for level, factors in result_dic.items():
                if level == "SEC" and system != 0:
                    continue
                if level == "EMS" and system != 1:
                    continue
                if level == "ES" and system != 2:
                    continue
                for factor in factors:
                    total_sum = []
                    # print(factor["name"])
                    for q in factor["question"]:
                        # print(q)
                        question = Question.objects.filter(
                            key=f"A-{level}-{q['id']}"
                        ).last()
                        answer = Answer.objects.filter(
                            school__emstype=system,
                            question_id=question.id,
                            answer__in=[question.get_option(qu) for qu in q["answer"]],
                        )
                        _sum = []
                        for a in answer:
                            # print(a.answer)
                            # print(a.frequency)
                            if a.answer in question.get_all_options():
                                _sum.append(a.frequency)
                        res = sum(_sum)
                        q = Question.objects.filter(
                            key__startswith=f"A-{level}"
                        ).first()
                        student_count = sum(
                            [
                                a.frequency
                                for a in Answer.objects.filter(
                                    school__emstype=system, question_id=q.id
                                )
                            ]
                        )
                        result = res / student_count
                        total_sum.append(round(result * 100, 2))
                    # print(total_sum)
                    total_res = round((sum(total_sum) / len(total_sum)), 2)
                    if total_res < factor["prevalence_min"][0]:
                        prevalence = 0
                        recomendation = "Enhorabuena, con esta conducta de riesgo tu plantel no tiene un problema."
                    elif (
                        total_res >= factor["prevalence_min"][0]
                        and total_res < factor["prevalence_min"][1]
                    ):
                        prevalence = 1
                        recomendation = factor["recomendation_min"]
                        if hasattr(factor, "prevalence_mid"):
                            social_health_score = social_health_score + 1
                    elif (
                        total_res >= factor["prevalence_mid"][0]
                        and total_res < factor["prevalence_mid"][1]
                    ):
                        prevalence = 2
                        recomendation = factor["recomendation_mid"]
                        social_health_score = social_health_score + 1
                    elif (
                        total_res >= factor["prevalence_high"][0]
                        and total_res < factor["prevalence_high"][1]
                    ):
                        prevalence = 3
                        recomendation = factor["recomendation_high"]
                        social_health_score = social_health_score + 1
                    elif (
                        total_res >= factor["prevalence_veryhigh"][0]
                        and total_res <= factor["prevalence_veryhigh"][1]
                    ):
                        prevalence = 4
                        recomendation = factor["recomendation_veryhigh"]
                        social_health_score = social_health_score + 1
                    data.append(
                        {
                            "factor": factor["name"],
                            "value": total_res,
                            "recomendation": recomendation if not simple else "",
                            "prevalence": prevalence,
                            "count": round(student_count * (total_res / 100)),
                        }
                    )
        ctx["system_name"] = system_name
        if system == 0:
            ctx["system_acronym"] = "SEC"
        elif system == 1:
            ctx["system_acronym"] = "EMS"
        elif system == 2:
            ctx["system_acronym"] = "ES"
        else:
            pass
        ctx["system"] = True

        # print(social_health_score)
        ctx["social_health"] = clamp_social_health(social_health_score)
        if not simple:
            if system == 0:
                t = "M-SEC" if is_teacher else "A-SEC"
                age = Answer.objects.filter(
                    school__emstype=system, question__key=f"{t}-1"
                )
                gender = Answer.objects.filter(
                    school__emstype=system, question__key=f"{t}-2"
                )
                work = Answer.objects.filter(
                    school__emstype=system, question__key=f"{t}-5"
                )
                home = Answer.objects.filter(
                    school__emstype=system, question__key=f"{t}-6"
                )
            elif system == 1:
                t = "D-EMS" if is_teacher else "A-EMS"
                age = Answer.objects.filter(
                    school__emstype=system, question__key=f"{t}-1"
                )
                gender = Answer.objects.filter(
                    school__emstype=system, question__key=f"{t}-2"
                )
                work = Answer.objects.filter(
                    school__emstype=system, question__key=f"{t}-5"
                )
                home = Answer.objects.filter(
                    school__emstype=system, question__key=f"{t}-6"
                )
            elif system == 2:
                t = "D-ES" if is_teacher else "A-ES"
                age = Answer.objects.filter(
                    school__emstype=system, question__key=f"{t}-1"
                )
                gender = Answer.objects.filter(
                    school__emstype=system, question__key=f"{t}-2"
                )
                work = Answer.objects.filter(
                    school__emstype=system, question__key=f"{t}-5"
                )
                home = Answer.objects.filter(
                    school__emstype=system, question__key=f"{t}-6"
                )
            # [question.get_option(qu) for qu in q['answer']]

            ctx["gender_data"] = get_count(gender)
            ctx["age_data"] = get_count(age)
            ctx["work_data"] = get_count(work)
            ctx["home_data"] = get_count(home)
    elif muni:

        ctx = {}
        data = []
        # for subsystem in subsystems:
        # for muni in municipality:
        if muni:
            if level == "SEC":
                emstype = 0
                query = Q(emstype=0)
                query_sh = Q(school__emstype=0)
            elif level == "EMS":
                emstype = 1
                query = Q(emstype=1)
                query_sh = Q(school__emstype=1)
            elif level == "ES":
                emstype = 2
                query = Q(emstype=2)
                query_sh = Q(school__emstype=2)

            school = School.objects.filter(Q(muni__id=muni.id) & query).first()
            # schools=School.objects.filter(emstype=2)
            data_teachers = {
                "SEC": [],
                "EMS": [],
                "ES": [],
                "DOC": [],
            }
            data_students = {
                "SEC": [],
                "EMS": [],
                "ES": [],
                "DOC": [],
            }
            dic = {}
            social_health_score = 0
            if school:

                # print(muni.name)
                # continue
                # for school in schools:
                # school=request.user.userapp.school
                # dic['subsystem']=subsystem.name
                # dic['subsystem_abrev']=subsystem.abrev
                dic["municipality"] = school.muni.name
                # dic['school_name']=school.school_name
                # dic['school_emstype'] = f'{school.get_emstype_display()} / {school.subsystem.abrev}'
                # dic['school_key'] = school.school_key
                if is_teacher:
                    for lvl, factors in result_teach.items():
                        for factor in factors:
                            total_sum = []
                            # print(factor["name"])
                            if emstype == 0:
                                t = "M-SEC"
                            if emstype == 1:
                                t = "D-EMS"
                            if emstype == 2:
                                t = "D-ES"
                            qt = Question.objects.filter(key__startswith=f"{t}").first()
                            teacher_count = sum(
                                [
                                    a.frequency
                                    for a in Answer.objects.filter(
                                        query_sh
                                        & Q(school__muni__id=muni.id)
                                        & Q(question_id=qt.id)
                                    )
                                ]
                            )
                            for q in factor["question"]:
                                key = f"{t}-{q['id']}"
                                # print(q)
                                question = Question.objects.filter(key=key).last()
                                # print(key)
                                # print(question)
                                # print(school.id)
                                # print(school.school_name)
                                # print(school.answers.all())
                                answer = Answer.objects.filter(
                                    query_sh
                                    & Q(question_id=question.id)
                                    & Q(school__muni__id=muni.id)
                                    & Q(
                                        answer__in=[
                                            question.get_option(qu)
                                            for qu in q["answer"]
                                        ]
                                    )
                                )
                                if not answer:
                                    continue
                                _sum = []
                                for a in answer:
                                    if a.answer in question.get_all_options():
                                        _sum.append(a.frequency)
                                res = sum(_sum)
                                result = res / teacher_count
                                total_sum.append(round(result * 100, 2))
                            if total_sum:
                                total_res = round((sum(total_sum) / len(total_sum)), 2)
                            else:
                                total_res = 0
                            # print(total_res)
                            try:
                                if total_res < factor["prevalence_min"][0]:
                                    prevalence = 0
                                    recomendation = "Enhorabuena, con esta conducta de riesgo tu plantel no tiene un problema."
                                elif (
                                    total_res >= factor["prevalence_min"][0]
                                    and total_res < factor["prevalence_min"][1]
                                ):
                                    prevalence = 1
                                    recomendation = factor["recomendation_min"]
                                    if hasattr(factor, "prevalence_mid"):
                                        social_health_score = social_health_score + 1
                                elif (
                                    total_res >= factor["prevalence_mid"][0]
                                    and total_res < factor["prevalence_mid"][1]
                                ):
                                    prevalence = 2
                                    recomendation = factor["recomendation_mid"]
                                    social_health_score = social_health_score + 1
                                elif (
                                    total_res >= factor["prevalence_high"][0]
                                    and total_res < factor["prevalence_high"][1]
                                ):
                                    prevalence = 3
                                    recomendation = factor["recomendation_high"]
                                    social_health_score = social_health_score + 1
                            except Exception as e:
                                print(school.id)
                                print(total_res)
                                print(e)
                            data_teachers["DOC"].append(
                                {
                                    "factor": factor["name"],
                                    "value": total_res,
                                    "recomendation": (
                                        recomendation if not simple else ""
                                    ),
                                    "count": round(teacher_count * (total_res / 100)),
                                    "prevalence": prevalence,
                                }
                            )
                else:
                    for lvl, factors in result_dic.items():
                        if lvl != level:
                            continue
                        if lvl == "SEC" and emstype != 0:
                            continue
                        if lvl == "EMS" and emstype != 1:
                            continue
                        if lvl == "ES" and emstype != 2:
                            continue
                        for factor in factors:
                            total_sum = []
                            # print(factor["name"])
                            qt = Question.objects.filter(
                                key__startswith=f"A-{lvl}"
                            ).first()
                            student_count = sum(
                                [
                                    a.frequency
                                    for a in Answer.objects.filter(
                                        school__muni__id=muni.id, question_id=qt.id
                                    )
                                ]
                            )
                            for q in factor["question"]:
                                # print(q)
                                question = Question.objects.filter(
                                    key=f"A-{lvl}-{q['id']}"
                                ).last()
                                answer = Answer.objects.filter(
                                    question_id=question.id,
                                    school__muni__id=muni.id,
                                    answer__in=[
                                        question.get_option(qu) for qu in q["answer"]
                                    ],
                                )
                                if not answer:
                                    continue
                                _sum = []
                                for a in answer:
                                    if a.answer in question.get_all_options():
                                        _sum.append(a.frequency)
                                res = sum(_sum)
                                result = res / student_count
                                total_sum.append(round(result * 100, 2))
                            if total_sum:
                                total_res = round((sum(total_sum) / len(total_sum)), 2)
                            else:
                                total_res = 0
                            if total_res < factor["prevalence_min"][0]:
                                prevalence = 0
                                recomendation = "Enhorabuena, con esta conducta de riesgo tu plantel no tiene un problema."
                            elif (
                                total_res >= factor["prevalence_min"][0]
                                and total_res < factor["prevalence_min"][1]
                            ):
                                prevalence = 1
                                recomendation = factor["recomendation_min"]
                                if hasattr(factor, "prevalence_mid"):
                                    social_health_score = social_health_score + 1
                            elif (
                                total_res >= factor["prevalence_mid"][0]
                                and total_res < factor["prevalence_mid"][1]
                            ):
                                prevalence = 2
                                recomendation = factor["recomendation_mid"]
                                social_health_score = social_health_score + 1
                            elif (
                                total_res >= factor["prevalence_high"][0]
                                and total_res < factor["prevalence_high"][1]
                            ):
                                prevalence = 3
                                recomendation = factor["recomendation_high"]
                                social_health_score = social_health_score + 1
                            elif "prevalence_veryhigh" in factor:
                                if (
                                    total_res >= factor["prevalence_veryhigh"][0]
                                    and total_res <= factor["prevalence_veryhigh"][1]
                                ):
                                    prevalence = 4
                                    recomendation = factor["recomendation_veryhigh"]
                                    social_health_score = social_health_score + 1
                            else:
                                prevalence = 3
                                recomendation = factor["recomendation_high"]
                                social_health_score = social_health_score + 1
                            data_students[lvl].append(
                                {
                                    "factor": factor["name"],
                                    "value": total_res,
                                    "recomendation": (
                                        recomendation if not simple else ""
                                    ),
                                    "count": round(student_count * (total_res / 100)),
                                    "prevalence": prevalence,
                                }
                            )

                # print(social_health_score)
                ctx["social_health"] = clamp_social_health(social_health_score)
                if not simple:
                    if school.emstype == 0:
                        t = "M-SEC" if is_teacher else "A-SEC"
                        age = Answer.objects.filter(
                            school__muni__id=muni.id, question__key=f"{t}-1"
                        )
                        gender = Answer.objects.filter(
                            school__muni__id=muni.id, question__key=f"{t}-2"
                        )
                        work = Answer.objects.filter(
                            school__muni__id=muni.id, question__key=f"{t}-5"
                        )
                        home = Answer.objects.filter(
                            school__muni__id=muni.id, question__key=f"{t}-6"
                        )
                    elif school.emstype == 1:
                        t = "D-EMS" if is_teacher else "A-EMS"
                        age = Answer.objects.filter(
                            school__muni__id=muni.id, question__key=f"{t}-1"
                        )
                        gender = Answer.objects.filter(
                            school__muni__id=muni.id, question__key=f"{t}-2"
                        )
                        work = Answer.objects.filter(
                            school__muni__id=muni.id, question__key=f"{t}-5"
                        )
                        home = Answer.objects.filter(
                            school__muni__id=muni.id, question__key=f"{t}-6"
                        )
                    elif school.emstype == 2:
                        t = "D-ES" if is_teacher else "A-ES"
                        age = Answer.objects.filter(
                            school__muni__id=muni.id, question__key=f"{t}-1"
                        )
                        gender = Answer.objects.filter(
                            school__muni__id=muni.id, question__key=f"{t}-2"
                        )
                        work = Answer.objects.filter(
                            school__muni__id=muni.id, question__key=f"{t}-5"
                        )
                        home = Answer.objects.filter(
                            school__muni__id=muni.id, question__key=f"{t}-6"
                        )
                    dic["gender_data"] = get_count(gender)
                    dic["age_data"] = get_count(age)
                    dic["work_data"] = get_count(work)
                    dic["home_data"] = get_count(home)

            dic["data_teachers"] = data_teachers
            dic["data_students"] = data_students
            data.append(dic)
            ctx["is_muni"] = True
            ctx["muni_name"] = muni.name
            ctx["muni_key"] = muni.key
    # print(data)

    ctx["is_teachers"] = is_teacher
    ctx["protector_factors"] = [
        "Relación alumnado - plantel",
        "Familiares",
        "Personales",
    ]
    ctx["level"] = level
    if student_teacher:
        ctx["data_teachers"] = data_teachers
    ctx["data"] = data
    # if dont exists save on model
    if not rresult.exists():
        print("saving results")
        rresult = ReportResult.objects.create(
            student_teacher=student_teacher,
            is_teacher=is_teacher,
            is_school=is_school,
            is_subsystem=is_subsystem,
            is_system=is_system,
            is_muni=is_muni,
            subsystem=subsystem,
            system=system,
            system_name=system_name,
            school=school,
            muni=muni,
            simple=simple,
            level=init_level,
            result=ctx,
        )
    return ctx


class ReportView(View):
    def get(self, request, *args, **kwargs):
        # if not request.user.is_superuser:
        #     return HttpResponseRedirect(reverse_lazy('main'))
        ctx = init_user(request.user.userapp)

        is_school = False
        is_subsystem = False
        is_system = False
        is_muni = False
        is_state = False
        is_teacher = False
        no_role = True
        try:
            if request.user.userapp.school:
                is_school = True
        except Exception as e:
            print(e)
        try:
            if request.user.userapp.subsystem:
                if "school" in request.GET:
                    is_school = True
                else:
                    is_subsystem = True
        except Exception as e:
            print(e)
        try:
            if request.user.userapp.level == 0:
                is_system = True
            elif request.user.userapp.level == 1:
                is_system = True
            elif request.user.userapp.level == 2:
                is_system = True
            elif request.user.userapp.level == 3:
                is_muni = True
            elif request.user.userapp.level == 4:
                is_state = True
        except Exception as e:
            print(e)
        print(is_system)
        print(is_muni)
        if "teachers" in kwargs:
            if kwargs["teachers"] == 1:
                is_teacher = True
        # print(request.user.userapp.level)
        # print(result_dic["factor-ems"])
        # print(is_school)
        # print(kwargs)
        if "level" in kwargs:
            if kwargs["level"] == "SEC":
                l = "SEC"
            elif kwargs["level"] == "EMS":
                l = "EMS"
            elif kwargs["level"] == "ES":
                l = "ES"
        else:
            l = "SEC"
        if is_school:
            if "school" in request.GET:
                sch_id = request.GET.get("school", None)
                if sch_id:
                    school = School.objects.filter(id=sch_id).last()
                else:
                    school = request.user.userapp.school
            else:
                school = request.user.userapp.school
            ctx = get_data(
                is_teacher=is_teacher, is_school=is_school, school=school, level=l
            )
            ctx["no_role"] = False
        if is_subsystem:
            school = request.user.userapp.subsystem.schools.first()
            subsystem = request.user.userapp.subsystem
            ctx = get_data(
                is_teacher=is_teacher,
                is_subsystem=is_subsystem,
                subsystem=subsystem,
                school=school,
                level=l,
            )
            ctx["no_role"] = False
        if is_system:
            school = School.objects.filter(emstype=request.user.userapp.level).first()
            system = request.user.userapp.level
            system_name = request.user.userapp.get_level_display()
            ctx = get_data(
                is_teacher=is_teacher,
                is_system=is_system,
                system=system,
                system_name=system_name,
                school=school,
                level=l,
            )
            ctx["no_role"] = False
        if is_muni:
            print("is_muni 1")
            print(is_muni)
            muni = request.user.userapp.municipality
            print(muni)
            ctx = get_data(is_teacher=is_teacher, is_muni=is_muni, muni=muni, level=l)

            ctx["no_role"] = False
        if is_state:
            state_level_map = {
                "SEC": (0, "Jalisco - Secundaria"),
                "EMS": (1, "Jalisco - Educación Media Superior"),
                "ES": (2, "Jalisco - Educación Superior"),
            }
            system, system_name = state_level_map.get(l, (0, "Jalisco - Secundaria"))
            school = School.objects.filter(emstype=system).first()
            ctx = get_data(
                is_teacher=is_teacher,
                is_system=True,
                system=system,
                system_name=system_name,
                school=school,
                level=l,
            )
            ctx["is_state"] = True
            ctx["state_name"] = "Jalisco"
            ctx["no_role"] = False
        if not any([is_school, is_subsystem, is_system, is_muni, is_state]):
            ctx.setdefault("data", [])
            ctx["no_role"] = True
        # try:
        #     if request.user.is_superuser:
        #         with open("export.json", "w") as f:
        #             json.dump(ctx,f,indent=4)
        # except Exception as e:
        #     print(e)
        # print(ctx)
        # print(data_teachers)
        # print(data_students)
        ctx["is_admin"] = False

        ctx["location"] = "Informe"
        ctx["location_name"] = "report"
        if request.user.id in ADMINS:
            ctx["is_admin"] = True
        print(ctx)
        return render(request, "dssh-informe.html", ctx)


# {'is_school': True, 'school_name': 'CECyTEH ATITALAQUIA', 'school_emstype': 'Media Superior / CECyTEH', 'school_key': '13ECTAAAA1', 'school_id': 4, 'muni_name': 'Atitalaquia', 'terminal_efficiency': 0.0, 'reprobation': 0.0, 'social_health': 461, 'gender_data': [{'frequency': 17, 'name': 'Femenino'}, {'frequency': 8, 'name': 'Masculino'}, {'frequency': 0, 'name': 'No binario'}], 'age_data': [{'frequency': 0, 'name': 'Menos de 25'}, {'frequency': 12, 'name': 'Entre 25 y 34'}, {'frequency': 6, 'name': 'Entre 35 y 44'}, {'frequency': 6, 'name': 'Entre 45 y 54'}, {'frequency': 1, 'name': 'Más de 54'}], 'work_data': [{'frequency': 8, 'name': 'Soy profesor.'}, {'frequency': 3, 'name': 'Soy directivo'}, {'frequency': 10, 'name': 'Soy empleado administrativo'}, {'frequency': 1, 'name': 'Soy prefecto'}, {'frequency': 3, 'name': 'Soy empleado de intendencia, o vigilancia'}], 'home_data': [{'frequency': 2, 'name': 'Vivo solo'}, {'frequency': 2, 'name': '1'}, {'frequency': 7, 'name': '2 - 3'}, {'frequency': 0, 'name': '4 - 5'}, {'frequency': 0, 'name': 'más de 5'}], 'is_teachers': True, 'protector_factors': ['Relación alumnado - plantel', 'Familiares', 'Personales'], 'level': 'DOC', 'data': [{'factor': 'Ansiedad', 'value': 26.67, 'recomendation': 'Se recomienda tener en el plantel y/o municipio, servicios específicos de ayuda en el manejo de la ansiedad, así como grupos terapéuticos. Ya que la tasa a partir de esta cifra es muy alta y se considera un agravante o detonante para la presencia de otros factores de riesgo como puede ser el burnout, la depresión, y el consumo de sustancias psicoactivas, así como para la violencia.', 'count': 7, 'prevalence': 3}, {'factor': 'Anhedonia', 'value': 9.78, 'recomendation': 'Es una tasa considerable donde conviene realizar campañas de actividades académicas, deportivas, culturales y laborales que despierten su curiosidad, como cursos de actualización, cursos de desarrollo personal o actividades que incluyan a todo el personal pero que sean recreativas.', 'count': 2, 'prevalence': 1}, {'factor': 'Depresión', 'value': 13.78, 'recomendation': 'Conviene realizar campañas sobre la depresión, ya que una de las secuelas de la pandemia ha sido la presencia de síntomas depresivos en toda la población, es importante trabajar junto con campañas de actividades académicas, deportivas y culturales que despierten la curiosidad, así como con campañas que hablen sobre las opciones de actualización laboral, desarrollo personal o cualquier otra actividad que pueda motivar a todo el personal.  También es indispensable una intervención más directa para explorar las relaciones familiares y la relación laboral del personal que presenta esta tasa.', 'count': 3, 'prevalence': 2}, {'factor': 'Vandalismo', 'value': 9.33, 'recomendation': 'La prevención de esta conducta en personas mayores de 20 años, no genera un efecto de persuasión, porque muy probablemente es un comportamiento que ya sea parte de su forma de socializar, por lo que hay que ir directamente a la intervención aplicando, sin excepciones, la normativa que se tenga o realizar una nueva que se adapte a las características de la población y genere vínculos con su comunidad de tal manera que les interese o les compense más tener comportamientos prosociales.', 'count': 2, 'prevalence': 1}, {'factor': 'Infracciones contra la propiedad', 'value': 8.0, 'recomendation': 'Este porcentaje que es significativo, sobre todo tratándose del personal docente y/o administrativo de una institución educativa pública. Por lo que es importante reinventar la relación del personal con su comunidad proporcionando espacios y herramientas que renueven la confianza y la participación de esta parte de la población. Población que puede generar conductas prosociales si en lugar de solamente castigarlos les proporcionamos alternativas para que no solamente se sientan productivos, sino que se conviertan en un recurso humano importante para la comunidad, además de la función que ya ejercen dentro de la institución.', 'count': 2, 'prevalence': 1}, {'factor': 'Consumo de estupefacientes (Drogas blandas)', 'value': 31.5, 'recomendation': 'Es recomendable desarrollar programas enfocados a informar y experimentar sobre las consecuencias del consumo de alcohol tanto a la población docente como a la población adulta en general. También es indispensable diseñar protocolos de acción dentro de los centros escolares y/o ayuntamientos, para lograr inhibir el consumo de alcohol, tabaco y marihuana. Así como ofrecer formación a los adultos del contexto inmediato para formar un mismo frente y evitar que se convierta en la puerta hacia el consumo de las drogas duras, o hacia otras conductas de riesgo como la conducción temeraria y las relaciones sexuales sin protección, o evitar que se conviertan en un modelo a seguir por el alumnado.', 'count': 8, 'prevalence': 2}, {'factor': 'Ideación suicida', 'value': 31.2, 'recomendation': 'Una tasa de prevalencia alta en este factor de riesgo implica que falta formación con relación al suicidio, y que hay una falta importante de recursos o de información sobre qué hacer si se tiene esta conducta o si conocemos a alguien que presenta ideación suicida. Por lo que es imperante hablar abiertamente en el entorno escolar sobre el suicidio, los recursos, cómo servir d e apoyo emocional en lo que se activan protocolos de seguridad, y sobre todo en la no banalización del tema cuando se trata del personal docente y no docente dentro de la institución educativa, ya que la cifra se pudo haber visto aumentada como una consecuencia de la pandemia.', 'count': 8, 'prevalence': 3}, {'factor': 'Adicción a las redes sociales', 'value': 32.0, 'recomendation': 'Esta tasa de prevalencia es esperable después de la pandemia, ya que gran parte de la socialización se tuvo que realizar a través de las redes sociales, lo que ha normalizado pasar muchas horas ante las redes sociales velando una posible adicción. Es recomendable desarrollar programas de manejo de adicciones a las redes sociales dentro de las Instituciones educativa. También se recomienda abrir espacios de ocio alternativo para que vuelvan a socializar sin depender de la tecnología y hacerlos participes de las campañas que se diseñen para combatir esta y otras adicciones detonadas por las redes sociales, como lo es la adicción a la pornografía. Pensando siempre en que el alumnado emula a los adultos de su entorno inmediato, es indispensable educar en el uso de las tecnologías a los adultos de su contexto inmediato.', 'count': 8, 'prevalence': 2}, {'factor': 'Control de impulsos', 'value': 40.0, 'recomendation': 'Una tasa de prevalencia que abarca más de la tercera parte de la población con un pobre control de impulsos, va a dificultar que la población más joven desarrolle esa misma competencia, ya que los docentes y no docentes de una institución educativa, suelen ser modelos a seguir; lo que nos daría una población mucho más laxa en su cultura de legalidad, más violenta y adictiva. Por lo que es recomendable reforzar los valores de la comunidad, teniendo normas y consecuencias muy claras y realizar campañas de concientización. Así como es recomendable desarrollar talleres de comunicación y autocontrol en la población adulta, así como el manejo de la frustración y, programas donde se trabaje la toma de decisiones, el tipo de respuestas que dan ante ciertas situaciones y la asertividad. Enseñándoles la importancia de tener la capacidad de aplazar y controlar los impulsos, aprendiendo a evaluar  costo – beneficio.', 'count': 10, 'prevalence': 3}, {'factor': 'Consumo de estupefacientes (Drogas duras)', 'value': 0.4, 'recomendation': 'Enhorabuena, con esta conducta de riesgo tu plantel no tiene un problema.', 'count': 0, 'prevalence': 0}, {'factor': 'Violencia', 'value': 6.91, 'recomendation': 'El personal docente y administrativo que presentan esta prevalencia tienen interiorizada la violencia física. Moldear esta conducta en adultos se vuelve más eficiente a través de consecuencias aversivas hacia este comportamiento, pero también con consecuencias positivas, como reconocimiento social a las personas que no la ejercen aun pudiendo hacerlo. Sobre todo, con una tasa de prevalencia que es significativa pero que no es alta.', 'count': 2, 'prevalence': 1}, {'factor': 'Acoso escolar', 'value': 16.0, 'recomendation': 'Cuando este comportamiento se manifiesta en el personal docente y no docente, se manifiesta que es una conducta interiorizada y normalizada en la institución educativa. Por lo que es indispensable que existan protocolos en los planteles y/o Ayuntamientos, normas claras con sus consecuencias sociales que manden el mensaje de que no hay tolerancia hacia este tipo de comportamientos, sobre todo tratándose de adultos que tienen como función ser un modelo social para el alumnado. Es importante especificar el tipo de comportamientos que implica el acoso escolar ya que muchas veces está tan normalizado que no se dan cuenta de que lo que están haciendo es Acoso escolar. Por lo que la formación con relación a este tema es indispensable en todo el personal de la Institución Educativa.', 'count': 4, 'prevalence': 2}, {'factor': 'Conductas antisociales', 'value': 13.33, 'recomendation': 'En la edad adulta es muy probable que ya empiece a definirse una carrera delictiva dentro del personal docente y no docente, lo importante con este porcentaje que es significativo, pero no demasiado alto, además de aplicar consecuencias por cometer actos predelictivos, será intervenir de manera directa en la población y promover talleres donde la misma población trabaje y haga conciencia de su cultura de legalidad y no permita la normalización de conductas delictivas a través de la resignación.', 'count': 3, 'prevalence': 1}, {'factor': 'Relación alumnado - plantel', 'value': 79.2, 'recomendation': 'Con una cuarta parte de los docentes y no docentes de una institución educativa, que tienen una buena relación con su plantel, tienen sensación de seguridad, consideran que su plantel está limpio, sienten que sus alumnos valoran su trabajo y su entorno laboral también,  y además sienten que confían en ellos, esto se convierte en uno de los factores protectores más fuertes frente a las conductas de riesgo. Indica que el plantel está cumpliendo como agente de socialización. Es muy buena noticia saber que el personal docente y no docente de la institución educativa se sienten en su plantel con la confianza de moverse libremente y de recurrir a alguien del entorno laboral en caso de tener algún problema, indica una comunicación horizontal reforzada y fomentada por todos y un buen ambiente de trabajo, además de una implicación de parte de la población adulta que convive con ellos todos los días. Lo que contribuye significativamente a la salud social del plantel y su entorno inmediato.', 'count': 20, 'prevalence': 1}, {'factor': 'Familiares', 'value': 59.43, 'recomendation': 'La comunicación familiar, así como las relaciones familiares son un factor protector vital, que determina el sistema de valores y creencias de todas las personas, así como su cultura de legalidad y la manera como se relacionarán con el entorno. Por lo que al tener más de la tercera parte de la población más joven que cuenta con una buena relación familiar, o que cuentan con padres que han estudiado, las probabilidades de desarrollar hábitos basados en conductas de riesgo son menores, por lo que una buena relación familiar es un factor protector determinante a la hora de paliar con los factores de riesgo que existen dentro y fuera del plantel.', 'count': 15, 'prevalence': 1}, {'factor': 'Personales', 'value': 91.2, 'recomendation': 'A partir de esta edad, tener un buen autoconcepto con relación a los demás, así como sentirse bien con su género en una cuarta parte de la población. Indica que está presente un factor de protección determinante a la hora de lidiar y mantener conductas de riesgo como las adicciones o la violencia. Así como también cumple una función reforzadora al ser una población que es emulada por lo más jóvenes.', 'count': 23, 'prevalence': 1}], 'no_role': False, 'is_admin': True, 'location': 'Informe', 'location_name': 'report'}


class MainView(LoginRequiredMixin, View):
    login_url = reverse_lazy("login")
    redirect_field_name = "redirect_to"

    def get(self, request, *args, **kwargs):
        ctx = {}
        if "msg" in kwargs:
            ctx["msg"] = kwargs["msg"]
        else:
            ctx["msg"] = ""
        user = request.user
        ctx["is_school"] = False
        ctx["is_school"] = False
        ctx["is_subsystem"] = False
        ctx["system"] = False
        ctx["is_muni"] = False
        ctx["is_state"] = False
        ctx["is_admin"] = False
        ctx["no_role"] = True

        def get_qst(emstype):
            if emstype == 0:
                question = Question.objects.filter(key__startswith="A-SEC").first()
                question2 = Question.objects.filter(key__startswith="M-SEC").first()
            elif emstype == 1:
                question = Question.objects.filter(key__startswith="A-EMS").first()
                question2 = Question.objects.filter(key__startswith="D-EMS").first()
            elif emstype == 2:
                question = Question.objects.filter(key__startswith="A-ES").first()
                question2 = Question.objects.filter(key__startswith="D-ES").first()
            return (question, question2)

        try:
            if user.userapp.school:
                ctx["is_school"] = True
                ctx["school_emstype"] = user.userapp.school.get_emstype_display()
                ctx["school_cct"] = user.userapp.school.school_key
                ctx["school_name"] = user.userapp.school.school_name
                q1, q2 = get_qst(user.userapp.school.emstype)
                ctx["student_count"] = sum(
                    [
                        a.frequency
                        for a in Answer.objects.filter(
                            school_id=user.userapp.school.id, question_id=q1.id
                        )
                    ]
                )
                ctx["teacher_count"] = sum(
                    [
                        a.frequency
                        for a in Answer.objects.filter(
                            school_id=user.userapp.school.id, question_id=q2.id
                        )
                    ]
                )
                ctx["group_count"] = Group.objects.filter(
                    school_id=user.userapp.school.id
                ).count()
                ctx["no_role"] = False
        except:
            pass
        try:
            if user.userapp.subsystem:
                subsystem = user.userapp.subsystem
                schools = subsystem.schools.all()
                ctx["is_subsystem"] = True
                ctx["subsystem_name"] = subsystem.name
                ctx["subsystem_abrev"] = subsystem.abrev
                ctx["subsystem_id"] = subsystem.id
                sh = subsystem.schools.order_by("-id").first()
                ctx["subsystem_school"] = sh
                q1, q2 = get_qst(sh.emstype)
                ctx["student_count"] = sum(
                    [
                        a.frequency
                        for a in Answer.objects.filter(
                            school__subsystem__id=subsystem.id, question_id=q1.id
                        )
                    ]
                )
                ctx["teacher_count"] = sum(
                    [
                        a.frequency
                        for a in Answer.objects.filter(
                            school__subsystem__id=subsystem.id, question_id=q2.id
                        )
                    ]
                )
                ctx["group_count"] = Group.objects.filter(
                    school__subsystem__id=subsystem.id
                ).count()
                ctx["no_role"] = False
        except Exception as e:

            print(e)
        try:
            if user.userapp.level == 0:
                ctx["system"] = True
                ctx["system_acronym"] = "SEC"
                ctx["system_name"] = "Secundaria"
                q1, q2 = get_qst(0)
                ctx["student_count"] = sum(
                    [
                        a.frequency
                        for a in Answer.objects.filter(
                            school__emstype=1, question_id=q1.id
                        )
                    ]
                )
                ctx["teacher_count"] = sum(
                    [
                        a.frequency
                        for a in Answer.objects.filter(
                            school__emstype=1, question_id=q2.id
                        )
                    ]
                )
                ctx["group_count"] = Group.objects.filter(school__emstype=0).count()
            elif user.userapp.level == 1:
                ctx["system"] = True
                ctx["system_acronym"] = "EMS"
                ctx["system_name"] = "Educación Media Superior"
                q1, q2 = get_qst(1)
                ctx["student_count"] = sum(
                    [
                        a.frequency
                        for a in Answer.objects.filter(
                            school__emstype=2, question_id=q1.id
                        )
                    ]
                )
                ctx["teacher_count"] = sum(
                    [
                        a.frequency
                        for a in Answer.objects.filter(
                            school__emstype=2, question_id=q2.id
                        )
                    ]
                )
                ctx["group_count"] = Group.objects.filter(school__emstype=1).count()
            elif user.userapp.level == 2:
                ctx["system"] = True
                ctx["system_acronym"] = "ES"
                ctx["system_name"] = "Educación Superior"
                q1, q2 = get_qst(2)
                ctx["student_count"] = sum(
                    [
                        a.frequency
                        for a in Answer.objects.filter(
                            school__emstype=0, question_id=q1.id
                        )
                    ]
                )
                ctx["teacher_count"] = sum(
                    [
                        a.frequency
                        for a in Answer.objects.filter(
                            school__emstype=0, question_id=q2.id
                        )
                    ]
                )
                ctx["group_count"] = Group.objects.filter(school__emstype=2).count()
            elif user.userapp.level == 3:
                ctx["is_muni"] = True
                ctx["muni_key"] = user.userapp.municipality.key
                ctx["muni_name"] = user.userapp.municipality.name
                ctx["muni_id"] = user.userapp.municipality.id
                sh = School.objects.filter(
                    muni__id=user.userapp.municipality.id
                ).first()
                ctx["subsystem_school"] = sh
                q1, q2 = get_qst(sh.emstype)
                ctx["student_count"] = sum(
                    [
                        a.frequency
                        for a in Answer.objects.filter(
                            school__muni__id=user.userapp.municipality.id,
                            question_id=q1.id,
                        )
                    ]
                )
                ctx["teacher_count"] = sum(
                    [
                        a.frequency
                        for a in Answer.objects.filter(
                            school__muni__id=user.userapp.municipality.id,
                            question_id=q2.id,
                        )
                    ]
                )
                ctx["group_count"] = Group.objects.filter(
                    school__muni__id=user.userapp.municipality.id
                ).count()
                ctx["school_count"] = School.objects.filter(
                    muni__id=user.userapp.municipality.id
                ).count()
                ctx["no_role"] = False
            elif user.userapp.level == 4:
                ctx["is_state"] = True
                ctx["state_name"] = "Jalisco"
                ctx["subsystem_school"] = School.objects.order_by("id").first()
                q1 = Question.objects.filter(key__startswith="A-SEC").first()
                q2 = Question.objects.filter(key__startswith="M-SEC").first()
                if q1:
                    ctx["student_count"] = sum(
                        [
                            a.frequency
                            for a in Answer.objects.filter(question_id=q1.id)
                        ]
                    )
                if q2:
                    ctx["teacher_count"] = sum(
                        [
                            a.frequency
                            for a in Answer.objects.filter(question_id=q2.id)
                        ]
                    )
                ctx["group_count"] = Group.objects.count()
                ctx["school_count"] = School.objects.count()
                ctx["no_role"] = False
            if user.userapp.level is not None and user.userapp.level in [0, 1, 2]:
                ctx["no_role"] = False
        except:
            pass

        print(ctx)
        if user.id in ADMINS:
            ctx["is_admin"] = True
        ctx["location"] = "Página Principal"
        ctx["location_name"] = "main"
        return render(request, "base.html", ctx)


class SurveyFormView(LoginRequiredMixin, View):
    login_url = reverse_lazy("login")
    redirect_field_name = "redirect_to"

    def get(self, request, *args, **kwargs):
        ctx = {}
        if "msg" in kwargs:
            ctx["msg"] = kwargs["msg"]
        else:
            ctx["msg"] = ""
        user = request.user
        try:
            ctx["school_name"] = user.userapp.school.school_name
            ctx["school_emstype"] = (
                f"{user.userapp.school.get_emstype_display()} / {user.userapp.school.subsystem.abrev}"
            )
            ctx["school_desc"] = f"{user.username} - {user.userapp.school.school_name}"
            ctx["is_school"] = True
        except Exception as e:
            print(e)
            ctx["school_name"] = user.get_full_name()
            ctx["is_school"] = False
        ctx["location"] = "Página Principal"
        ctx["location_name"] = "main"
        return render(request, "survey_form.html", ctx)


def get_student_file(request):
    # get file location
    if not hasattr(request.user.userapp, "school"):
        return HttpResponseNotFound("File not found")
    subsystem = request.user.userapp.school.subsystem.abrev
    # school_key=request.user.username
    school_key = request.user.userapp.school.school_key
    school_name = request.user.userapp.school.school_name
    filename = f"{school_key} - {school_name}.xlsx"
    path = f"media/ralumnos/{subsystem}/{school_key} - {school_name}.xlsx"
    response = FileResponse(open(path, "rb"), content_type="application/xlsx")
    response["Content-Disposition"] = f"attachment; filename={filename}"
    return response


def get_teacher_file(request):
    # get file location
    if not hasattr(request.user.userapp, "school"):
        return HttpResponseNotFound("File not found")
    subsystem = request.user.userapp.school.subsystem.abrev
    # school_key=request.user.username
    school_key = request.user.userapp.school.school_key
    school_name = request.user.userapp.school.school_name
    filename = f"{school_key} - {school_name}.xlsx"
    path = f"media/rdocentes/{subsystem}/{school_key} - {school_name}.xlsx"
    response = FileResponse(open(path, "rb"), content_type="application/xlsx")
    response["Content-Disposition"] = f"attachment; filename={filename}"
    return response


def gen_users():
    users = []
    # for school in list(set([item.school_key for item in School.objects.all()])):
    schools = [
        "13DES0062G-TM",
        "13DES0062G-TV",
        "13DES0063F-TM",
        "13DES0063F-TV",
        "13DST0013Q-TM",
        "13DST0013Q-TV",
        "13DES0044R-TM",
        "13DES0044R-TV",
        "13DST0070H-TM",
        "13DST0070H-TV",
        "13DES0011Z-TM",
        "13DES0011Z-TV",
        "13DST0028S-TM",
        "13DST0028S-TV",
        "13DES0009L-TM",
        "13DES0009L-TV",
        "13DES0041U-TM",
        "13DES0041U-TV",
        "13DST0033D-TM",
        "13DST0033D-TV",
        "13DES0064E-TM",
        "13DES0064E-TV",
        "13DES0010A-TM",
        "13DES0010A-TV",
        "13DES0070P-TM",
        "13DES0070P-TV",
        "13DES0106N-TM",
        "13DES0106N-TV",
        "13DES0107M-TM",
        "13DES0107M-TV",
        "13DST0051T-TM",
        "13DST0051T-TV",
        "13DST0062Z-TM",
        "13DST0062Z-TV",
        "13DST0076B-TM",
        "13DST0076B-TV",
        "13DES0024D-TM",
        "13DES0024D-TV",
        "13DES0065D-TM",
        "13DES0065D-TV",
        "13DES0012Z-TM",
        "13DES0012Z-TV",
        "13DES0014X-TM",
        "13DES0014X-TV",
        "13DES0018T-TM",
        "13DES0018T-TV",
        "13DES0030O-TM",
        "13DES0030O-TV",
        "13DES0092A-TM",
        "13DES0092A-TV",
        "13DES0103Q-TM",
        "13DES0103Q-TV",
        "13DST0001L-TM",
        "13DST0001L-TV",
        "13DST0040N-TM",
        "13DST0040N-TV",
        "13DST0055P-TM",
        "13DST0055P-TV",
        "13DST0071G-TM",
        "13DST0071G-TV",
        "13DES0022F-TM",
        "13DES0022F-TV",
        "13DES0043S-TM",
        "13DES0043S-TV",
        "13DES0031N-TM",
        "13DES0031N-TV",
        "13DES0017U-TM",
        "13DES0017U-TV",
        "13DST0026U-TM",
        "13DST0026U-TV",
        "13DST0012R-TM",
        "13DST0012R-TV",
        "13DST0056O-TM",
        "13DST0056O-TV",
        "13DES0033L-TM",
        "13DES0033L-TV",
        "13DES0015W-TM",
        "13DES0015W-TV",
        "13DES0084S-TM",
        "13DES0084S-TV",
        "13DES0115V-TM",
        "13DES0115V-TV",
        "13DES0118S-TM",
        "13DES0118S-TV",
        "13DES0125B-TM",
        "13DES0125B-TV",
        "13DST0052S-TM",
        "13DST0052S-TV",
        "13DES0029Z-TM",
        "13DES0029Z-TV",
        "13DES0013Y-TM",
        "13DES0013Y-TV",
        "13DES0005P-TM",
        "13DES0005P-TV",
        "13DES0034K-TM",
        "13DES0034K-TV",
        "13DST0053R-TM",
        "13DST0053R-TV",
        "13DES0021G-TM",
        "13DES0021G-TV",
        "13DES0035J-TM",
        "13DES0035J-TV",
        "13DES0057V-TM",
        "13DES0057V-TV",
        "13DST0050U-TM",
        "13DST0050U-TV",
        "13DES0008M-TM",
        "13DES0008M-TV",
        "13DES0006O-TM",
        "13DES0006O-TV",
    ]
    for school in schools:
        if not User.objects.filter(username=school).exists():
            try:
                password = (User.objects.make_random_password(),)
                usr = User(
                    username=school,
                    first_name=password,
                    email="",
                    is_active=True,
                )
                usr.set_password(password)
                users.append(usr)
            except Exception as e:
                print(e)
                print(school)
    new_users = User.objects.bulk_create(users)


def import_answers(request):
    if request.method == "POST":
        excel_file = request.FILES["file"]
        # wb = openpyxl.load_workbook(filename=excel_file,data_only=True)

        # # getting a particular sheet by name out of many sheets
        # # p_.error(wb)
        # worksheet = wb.active
        # # p_.error(worksheet)
        # #get headers
        # schools={}
        # # p_.error(time.process_time())
        # print("start")
        # for col in worksheet.iter_cols(max_row=1):
        #     for cell in col:
        #         if cell.value=="ID_PLANTEL":
        #             for row in worksheet.iter_rows(
        #                 min_col=cell.column,max_col=cell.column,min_row=2):
        #                 val=row[0].value
        #                 if val in schools:
        #                     if row[0].row not in schools[val]:
        #                         schools[val].append(row[0].row)
        #                 else:
        #                     schools[val]=[row[0].row]
        #             break
        # # p_.error(time.process_time())
        # for col in worksheet.iter_cols(min_col=5,max_row=1):
        #     results={}
        #     for cell in col:
        #         # print(cell.value)
        #         try:
        #             splitted_cell=cell.value.split('-')
        #             question_id=f"{splitted_cell[0]}-{splitted_cell[1]}"
        #             # surveys=[sur.survey_name for sur in Survey.objects.all().only('survey_name') if sur.survey_name.startswith(question_id)]
        #         except:
        #             question_id=None
        #             # surveys=[]
        #         # if len(surveys)>0:
        #         # print(question_id)
        #         if Survey.objects.filter(survey_name__startswith=question_id).exists():
        #             # pass
        #             for school_id,rows in schools.items():
        #                 # print(f"{school_id}")
        #                 # get first and last row num
        #                 #find school in results
        #                 # if school_id in results:
        #                 # get values of first question
        #                 values=[row[0] for row in worksheet.iter_rows(
        #                     min_col=cell.column,max_col=cell.column,
        #                     min_row=rows[0],max_row=rows[-1],
        #                     values_only=True
        #                 ) if row[0]]
        #                 # worksheet[f"{cell.coordinate}"]
        #                 sch=School.objects.filter(id=school_id).last()
        #                 qst=Question.objects.filter(key=cell.value).last()
        #                 def update_answer(school, group,question,answer,frequency):
        #                     defa={
        #                             "school":school,
        #                             "group":group,
        #                             "question":question,
        #                             "answer":answer,
        #                             "frequency":frequency
        #                         }
        #                     obj, created = Answer.objects.update_or_create(
        #                         school=school, group=group,question=question,answer=answer,
        #                         defaults=defa,
        #                     )
        #                     if created:
        #                         return obj
        #                     else:
        #                         return None
        #                 if qst.option_a:
        #                     update_answer(school=sch, group=None,question=qst,answer=qst.option_a,frequency=values.count('A'))
        #                 if qst.option_b:
        #                     update_answer(school=sch, group=None,question=qst,answer=qst.option_b,frequency=values.count('B'))
        #                 if qst.option_c:
        #                     update_answer(school=sch, group=None,question=qst,answer=qst.option_c,frequency=values.count('C'))
        #                 if qst.option_d:
        #                     update_answer(school=sch, group=None,question=qst,answer=qst.option_d,frequency=values.count('D'))
        #                 if qst.option_e:
        #                     update_answer(school=sch, group=None,question=qst,answer=qst.option_e,frequency=values.count('E'))
        #             print(cell.value)
        #         else:
        #             continue

        # p_.error(time.process_time(),col)
        location = "media/temp/excel.xlsx"
        if not os.path.exists("media"):
            os.mkdir("media")
        if not os.path.exists("media/temp"):
            os.mkdir("media/temp")
        with open(location, "wb+") as destination:
            for chunk in excel_file.chunks():
                destination.write(chunk)
        print("starting task")
        save_excel_data(location)
        print("task end")
        return JsonResponse({"data": "ok"})


def get_questions(request):
    is_school = request.GET.get("survey_type", None)
    subsystem_school = request.GET.get("subsystem", None)
    muni = request.GET.get("muni", None)
    if subsystem_school:
        school = School.objects.filter(id=subsystem_school).last()
        if school.emstype == 0:
            key = "A-SEC" if is_school == "0" else "M-SEC"
        elif school.emstype == 1:
            key = "A-EMS" if is_school == "0" else "D-EMS"
        elif school.emstype == 2:
            key = "A-ES" if is_school == "0" else "D-ES"
        results = [
            {"id": item.id, "text": f"{item.key} - {item.question}"}
            for item in Question.objects.filter(key__startswith=key).order_by("id")
        ]
    elif muni:
        school = School.objects.filter(id=muni).last()
        print(school.school_name)
        if school.emstype == 0:
            key = "A-SEC" if is_school == "0" else "M-SEC"
        elif school.emstype == 1:
            key = "A-EMS" if is_school == "0" else "D-EMS"
        elif school.emstype == 2:
            key = "A-ES" if is_school == "0" else "D-ES"
        results = [
            {"id": item.id, "text": f"{item.key} - {item.question}"}
            for item in Question.objects.filter(key__startswith=key).order_by("id")
        ]
    elif request.user.userapp.school:
        if request.user.userapp.school.emstype == 0:
            key = "A-SEC" if is_school == "0" else "M-SEC"
        elif request.user.userapp.school.emstype == 1:
            key = "A-EMS" if is_school == "0" else "D-EMS"
        elif request.user.userapp.school.emstype == 2:
            key = "A-ES" if is_school == "0" else "D-ES"
        results = [
            {"id": item.id, "text": f"{item.key} - {item.question}"}
            for item in Question.objects.filter(key__startswith=key).order_by("id")
        ]
    else:
        results = []
    return JsonResponse({"data": results})


def get_schools(request):
    if request.user.id in ADMINS:
        results = [
            {"id": item.id, "text": f"{item.school_key} - {item.school_name}"}
            for item in School.objects.all().order_by("id")
        ]
    elif request.user.userapp.subsystem:
        results = [
            {"id": item.id, "text": f"{item.school_key} - {item.school_name}"}
            for item in School.objects.filter(
                subsystem__id=request.user.userapp.subsystem.id
            ).order_by("id")
        ]
    elif request.user.userapp.level == 3:
        results = [
            {"id": item.id, "text": f"{item.school_key} - {item.school_name}"}
            for item in School.objects.filter(
                muni__id=request.user.userapp.municipality.id
            ).order_by("id")
        ]
    elif request.user.userapp.level == 4:
        results = [
            {"id": item.id, "text": f"{item.school_key} - {item.school_name}"}
            for item in School.objects.all().order_by("id")
        ]
    else:
        results = []
    return JsonResponse({"data": results})


def get_muni(request):
    results = [
        {"id": item.id, "text": f"{item.key} - {item.name}"}
        for item in Municipality.objects.all().order_by("id")
    ]
    return JsonResponse({"data": results})


def get_subsystem(request):
    level = request.GET.get("level")
    if level in MAP_LEVEL_CONFIG:
        query = _resolve_map_subsystem(level)
    else:
        query = Subsystem.objects.all().order_by("id")
    results = [
        {"id": item.id, "text": f"{item.abrev} - {item.name}"}
        for item in query
    ]
    return JsonResponse({"data": results})


def get_map_data(request):
    level = request.GET.get("level", "SEC")
    if level not in MAP_LEVEL_CONFIG:
        return JsonResponse({"error": "Nivel no válido."}, status=400)

    subsystem = None
    subsystem_id = request.GET.get("subsystem_id")
    if subsystem_id:
        subsystem = Subsystem.objects.filter(id=subsystem_id).first()
        if not subsystem:
            return JsonResponse({"error": "Subsistema no encontrado."}, status=404)

    geojson = _build_jalisco_map_response(level, subsystem=subsystem)
    return JsonResponse(
        {
            "data": geojson,
            "filters": {
                "level": level,
                "subsystem": subsystem.name if subsystem else None,
            },
        }
    )


def get_chart_data(request):
    # [
    #     {x: 'Masculino',  y: 418},
    #     {x: 'Femenino',   y: 249},
    #     {x: 'No binario', y: 584}
    #   ]
    question = request.GET.get("question_id", None)
    subsystem_school = request.GET.get("subsystem", None)
    if subsystem_school is not None:
        query = Answer.objects.filter(question__id=question, school_id=subsystem_school)
    elif request.user.userapp.school:
        print("has school")
        query = Answer.objects.filter(
            question__id=question, school_id=request.user.userapp.school.id
        )
        print(query)
    # school=School.objects.filter(school_key=request.user.username)
    # try:
    #     if school.exists():
    #     else:
    #         query=Answer.objects.filter(question_id=question,school_id=request.user.userapp.school.id)
    # except:
    #     query=Answer.objects.filter(question_id=question)
    try:
        name = query.last().question.question
    except:
        name = ""
    print(name)
    results = [{"x": item.answer, "y": int(item.frequency)} for item in query]
    print(results)
    return JsonResponse({"data": results, "name": name})


def change_role(request):
    if request.user.id in ADMINS:
        user = request.user.userapp
        school = request.GET.get("school", None)
        muni = request.GET.get("muni", None)
        subsystem = request.GET.get("subsystem", None)
        level = request.GET.get("level", None)
        if school:
            user.school = School.objects.get(id=school)
            user.subsystem = None
            user.municipality = None
            user.level = None
            user.save()
        elif level:  # system
            user.school = None
            user.subsystem = None
            user.municipality = None
            user.level = int(level)
            user.save()
        elif subsystem:  # subsystem
            user.school = None
            user.subsystem = Subsystem.objects.get(id=subsystem)
            user.municipality = None
            user.level = None
            user.save()
        elif muni:  # muni
            user.school = None
            user.subsystem = None
            user.municipality = Municipality.objects.get(id=muni)
            user.level = 3
            user.save()
        return JsonResponse({"status": "ok"})
    else:
        return JsonResponse({"status": "error"})

from django.template.loader import render_to_string
from django.conf import settings


def get_pdf_school():
    # school=School.objects.all().first()
    # municipality=Municipality.objects.all()
    subsystems = Subsystem.objects.all()
    ctx = {}
    data = []
    # for muni in municipality:
    for subsystem in subsystems:
        school = School.objects.filter(subsystem__id=subsystem.id, emstype=0).first()
        # schools=School.objects.filter(emstype=2)

        if not school:
            continue
        # for school in schools:
        # school=request.user.userapp.school
        data_teachers = {
            "SEC": [],
            "EMS": [],
            "ES": [],
            "DOC": [],
        }
        data_students = {
            "SEC": [],
            "EMS": [],
            "ES": [],
            "DOC": [],
        }
        dic = {}
        social_health_score = 0
        social_health_score_t = 0
        dic["subsystem"] = subsystem.name
        dic["subsystem_abrev"] = subsystem.abrev
        # dic['municipality']=school.muni.name
        # dic['school_name']=school.school_name
        # dic['school_emstype'] = f'{school.get_emstype_display()} / {school.subsystem.abrev}'
        # dic['school_key'] = school.school_key
        for level, factors in result_teach.items():
            for factor in factors:
                total_sum = []
                # print(factor["name"])
                if school.emstype == 0:
                    t = "M-SEC"
                if school.emstype == 1:
                    t = "D-EMS"
                if school.emstype == 2:
                    t = "D-ES"
                qt = Question.objects.filter(key__startswith=f"{t}").first()
                teacher_count = sum(
                    [
                        a.frequency
                        for a in Answer.objects.filter(
                            school__emstype=2,
                            school__subsystem__id=subsystem.id,
                            question_id=qt.id,
                        )
                    ]
                )
                for q in factor["question"]:
                    key = f"{t}-{q['id']}"
                    # print(q)
                    question = Question.objects.filter(key=key).last()
                    # print(key)
                    # print(question)
                    # print(school.id)
                    # print(school.school_name)
                    # print(school.answers.all())
                    answer = Answer.objects.filter(
                        Q(question_id=question.id)
                        & Q(school__emstype=2)
                        & Q(school__subsystem__id=subsystem.id)
                        & Q(answer__in=[question.get_option(qu) for qu in q["answer"]])
                    )
                    if not answer:
                        continue
                    _sum = []
                    for a in answer:
                        if a.answer in question.get_all_options():
                            _sum.append(a.frequency)
                    res = sum(_sum)
                    result = res / teacher_count
                    total_sum.append(round(result * 100, 2))
                if total_sum:
                    total_res = round((sum(total_sum) / len(total_sum)), 2)
                else:
                    total_res = 0
                # print(total_res)
                try:
                    if total_res < factor["prevalence_min"][0]:
                        prevalence = 0
                        recomendation = "Enhorabuena, con esta conducta de riesgo tu plantel no tiene un problema."
                    elif (
                        total_res >= factor["prevalence_min"][0]
                        and total_res < factor["prevalence_min"][1]
                    ):
                        prevalence = 1
                        recomendation = factor["recomendation_min"]
                        if hasattr(factor, "prevalence_mid"):
                            social_health_score_t = social_health_score_t + 1
                    elif (
                        total_res >= factor["prevalence_mid"][0]
                        and total_res < factor["prevalence_mid"][1]
                    ):
                        prevalence = 2
                        recomendation = factor["recomendation_mid"]
                        social_health_score_t = social_health_score_t + 1
                    elif (
                        total_res >= factor["prevalence_high"][0]
                        and total_res < factor["prevalence_high"][1]
                    ):
                        prevalence = 3
                        recomendation = factor["recomendation_high"]
                        social_health_score_t = social_health_score_t + 1
                except Exception as e:
                    print(school.id)
                    print(total_res)
                    print(e)
                data_teachers[level].append(
                    {
                        "factor": factor["name"],
                        "value": total_res,
                        "recomendation": recomendation,
                        "count": round(teacher_count * (total_res / 100)),
                        "prevalence": prevalence,
                    }
                )
        for level, factors in result_dic.items():
            if level != "ES":
                continue
            if level == "SEC" and school.emstype != 0:
                continue
            if level == "EMS" and school.emstype != 1:
                continue
            if level == "ES" and school.emstype != 2:
                continue
            for factor in factors:
                total_sum = []
                # print(factor["name"])
                qt = Question.objects.filter(key__startswith=f"A-{level}").first()
                student_count = sum(
                    [
                        a.frequency
                        for a in Answer.objects.filter(
                            question_id=qt.id, school__subsystem__id=subsystem.id
                        )
                    ]
                )
                for q in factor["question"]:
                    # print(q)
                    question = Question.objects.filter(
                        key=f"A-{level}-{q['id']}"
                    ).last()
                    answer = Answer.objects.filter(
                        question_id=question.id,
                        school__subsystem__id=subsystem.id,
                        answer__in=[question.get_option(qu) for qu in q["answer"]],
                    )
                    if not answer:
                        continue
                    _sum = []
                    for a in answer:
                        if a.answer in question.get_all_options():
                            _sum.append(a.frequency)
                    res = sum(_sum)
                    result = res / student_count
                    total_sum.append(round(result * 100, 2))
                if total_sum:
                    total_res = round((sum(total_sum) / len(total_sum)), 2)
                else:
                    total_res = 0
                if total_res < factor["prevalence_min"][0]:
                    prevalence = 0
                    recomendation = "Enhorabuena, con esta conducta de riesgo tu plantel no tiene un problema."
                elif (
                    total_res >= factor["prevalence_min"][0]
                    and total_res < factor["prevalence_min"][1]
                ):
                    prevalence = 1
                    recomendation = factor["recomendation_min"]
                    if hasattr(factor, "prevalence_mid"):
                        social_health_score = social_health_score + 1
                elif (
                    total_res >= factor["prevalence_mid"][0]
                    and total_res < factor["prevalence_mid"][1]
                ):
                    prevalence = 2
                    recomendation = factor["recomendation_mid"]
                    social_health_score = social_health_score + 1
                elif (
                    total_res >= factor["prevalence_high"][0]
                    and total_res < factor["prevalence_high"][1]
                ):
                    prevalence = 3
                    recomendation = factor["recomendation_high"]
                    social_health_score = social_health_score + 1
                elif "prevalence_veryhigh" in factor:
                    if (
                        total_res >= factor["prevalence_veryhigh"][0]
                        and total_res <= factor["prevalence_veryhigh"][1]
                    ):
                        prevalence = 4
                        recomendation = factor["recomendation_veryhigh"]
                        social_health_score = social_health_score + 1
                else:
                    prevalence = 3
                    recomendation = factor["recomendation_high"]
                    social_health_score = social_health_score + 1
                data_students[level].append(
                    {
                        "factor": factor["name"],
                        "value": total_res,
                        "recomendation": recomendation,
                        "count": round(student_count * (total_res / 100)),
                        "prevalence": prevalence,
                    }
                )

        # print(social_health_score)
        dic["social_health_stud"] = 1000 - (77 * social_health_score)
        dic["social_health_teach"] = 1000 - (77 * social_health_score_t)
        if school.emstype == 0:
            t = "A-SEC"
            age = school.answers.filter(question__key=f"{t}-1")
            gender = school.answers.filter(question__key=f"{t}-2")
            work = school.answers.filter(question__key=f"{t}-5")
            home = school.answers.filter(question__key=f"{t}-6")
        elif school.emstype == 1:
            t = "A-EMS"
            age = school.answers.filter(question__key=f"{t}-1")
            gender = school.answers.filter(question__key=f"{t}-2")
            work = school.answers.filter(question__key=f"{t}-5")
            home = school.answers.filter(question__key=f"{t}-6")
        elif school.emstype == 2:
            t = "A-ES"
            age = school.answers.filter(question__key=f"{t}-1")
            gender = school.answers.filter(question__key=f"{t}-2")
            work = school.answers.filter(question__key=f"{t}-5")
            home = school.answers.filter(question__key=f"{t}-6")
        dic["gender_data_stud"] = [
            {"frequency": answer.frequency, "name": answer.answer}
            for answer in gender.order_by("question__key")
        ]
        dic["age_data_stud"] = [
            {"frequency": answer.frequency, "name": answer.answer}
            for answer in age.order_by("question__key")
        ]
        dic["work_data_stud"] = [
            {"frequency": answer.frequency, "name": answer.answer}
            for answer in work.order_by("question__key")
        ]
        dic["home_data_stud"] = [
            {"frequency": answer.frequency, "name": answer.answer}
            for answer in home.order_by("question__key")
        ]
        if school.emstype == 0:
            t = "M-SEC"
            age = school.answers.filter(question__key=f"{t}-1")
            gender = school.answers.filter(question__key=f"{t}-2")
            work = school.answers.filter(question__key=f"{t}-5")
            home = school.answers.filter(question__key=f"{t}-6")
        elif school.emstype == 1:
            t = "D-EMS"
            age = school.answers.filter(question__key=f"{t}-1")
            gender = school.answers.filter(question__key=f"{t}-2")
            work = school.answers.filter(question__key=f"{t}-5")
            home = school.answers.filter(question__key=f"{t}-6")
        elif school.emstype == 2:
            t = "D-ES"
            age = school.answers.filter(question__key=f"{t}-1")
            gender = school.answers.filter(question__key=f"{t}-2")
            work = school.answers.filter(question__key=f"{t}-5")
            home = school.answers.filter(question__key=f"{t}-6")
        dic["gender_data_teach"] = [
            {"frequency": answer.frequency, "name": answer.answer}
            for answer in gender.order_by("question__key")
        ]
        dic["age_data_teach"] = [
            {"frequency": answer.frequency, "name": answer.answer}
            for answer in age.order_by("question__key")
        ]
        dic["work_data_teach"] = [
            {"frequency": answer.frequency, "name": answer.answer}
            for answer in work.order_by("question__key")
        ]
        dic["home_data_teach"] = [
            {"frequency": answer.frequency, "name": answer.answer}
            for answer in home.order_by("question__key")
        ]
        dic["data_teachers"] = data_teachers
        dic["data_students"] = data_students
        data.append(dic)
    ctx["data"] = data
    # task=pdf_reports_task.delay(request.user.id,ctx,'cedula_plantel')
    # if not os.path.exists(f"{settings.BASE_DIR}/media/tmp/{request.user.id}/cedula_plantel.pdf"):
    #     return JsonResponse({"status":"error"},status=400)
    # return JsonResponse({"status":'PDF generado con éxito',"pdf":_public_url(f"/media/tmp/{request.user.id}/cedula_plantel.pdf")})
    return {"status": "ok", "info": ctx}


def get_pdf_mun():
    # school=School.objects.all().first()
    municipality = Municipality.objects.all()
    # subsystems=Subsystem.objects.all()
    ctx = {}
    data = []
    # for subsystem in subsystems:
    for muni in municipality:
        school = School.objects.filter(muni__id=muni.id, emstype=2).first()
        # schools=School.objects.filter(emstype=2)

        if not school:
            print(muni.name)
            continue
        # for school in schools:
        # school=request.user.userapp.school
        data_teachers = {
            "SEC": [],
            "EMS": [],
            "ES": [],
            "DOC": [],
        }
        data_students = {
            "SEC": [],
            "EMS": [],
            "ES": [],
            "DOC": [],
        }
        dic = {}
        social_health_score = 0
        social_health_score_t = 0
        # dic['subsystem']=subsystem.name
        # dic['subsystem_abrev']=subsystem.abrev
        dic["municipality"] = school.muni.name
        # dic['school_name']=school.school_name
        # dic['school_emstype'] = f'{school.get_emstype_display()} / {school.subsystem.abrev}'
        # dic['school_key'] = school.school_key
        for level, factors in result_teach.items():
            if level != "DOC":
                continue
            for factor in factors:
                total_sum = []
                # print(factor["name"])
                if school.emstype == 0:
                    t = "M-SEC"
                if school.emstype == 1:
                    t = "D-EMS"
                if school.emstype == 2:
                    t = "D-ES"
                qt = Question.objects.filter(key__startswith=f"{t}").first()
                teacher_count = sum(
                    [
                        a.frequency
                        for a in Answer.objects.filter(
                            school__emstype=2,
                            school__muni__id=muni.id,
                            question_id=qt.id,
                        )
                    ]
                )
                for q in factor["question"]:
                    key = f"{t}-{q['id']}"
                    # print(q)
                    question = Question.objects.filter(key=key).last()
                    # print(key)
                    # print(question)
                    # print(school.id)
                    # print(school.school_name)
                    # print(school.answers.all())
                    answer = Answer.objects.filter(
                        Q(question_id=question.id)
                        & Q(school__emstype=2, school__muni__id=muni.id)
                        & Q(answer__in=[question.get_option(qu) for qu in q["answer"]])
                    )
                    if not answer:
                        continue
                    _sum = []
                    for a in answer:
                        if a.answer in question.get_all_options():
                            _sum.append(a.frequency)
                    res = sum(_sum)
                    result = res / teacher_count
                    total_sum.append(round(result * 100, 2))
                if total_sum:
                    total_res = round((sum(total_sum) / len(total_sum)), 2)
                else:
                    total_res = 0
                # print(total_res)
                try:
                    if total_res < factor["prevalence_min"][0]:
                        prevalence = 0
                        recomendation = "Enhorabuena, con esta conducta de riesgo tu plantel no tiene un problema."
                    elif (
                        total_res >= factor["prevalence_min"][0]
                        and total_res < factor["prevalence_min"][1]
                    ):
                        prevalence = 1
                        recomendation = factor["recomendation_min"]
                        if hasattr(factor, "prevalence_mid"):
                            social_health_score_t = social_health_score_t + 1
                    elif (
                        total_res >= factor["prevalence_mid"][0]
                        and total_res < factor["prevalence_mid"][1]
                    ):
                        prevalence = 2
                        recomendation = factor["recomendation_mid"]
                        social_health_score_t = social_health_score_t + 1
                    elif (
                        total_res >= factor["prevalence_high"][0]
                        and total_res < factor["prevalence_high"][1]
                    ):
                        prevalence = 3
                        recomendation = factor["recomendation_high"]
                        social_health_score_t = social_health_score_t + 1
                except Exception as e:
                    print(school.id)
                    print(total_res)
                    print(e)
                data_teachers[level].append(
                    {
                        "factor": factor["name"],
                        "value": total_res,
                        "recomendation": recomendation,
                        "count": round(teacher_count * (total_res / 100)),
                        "prevalence": prevalence,
                    }
                )
        for level, factors in result_dic.items():
            if level != "ES":
                continue
            if level == "SEC" and school.emstype != 0:
                continue
            if level == "EMS" and school.emstype != 1:
                continue
            if level == "ES" and school.emstype != 2:
                continue
            for factor in factors:
                total_sum = []
                # print(factor["name"])
                qt = Question.objects.filter(key__startswith=f"A-{level}").first()
                student_count = sum(
                    [
                        a.frequency
                        for a in Answer.objects.filter(
                            school__muni__id=muni.id, question_id=qt.id
                        )
                    ]
                )
                for q in factor["question"]:
                    # print(q)
                    question = Question.objects.filter(
                        key=f"A-{level}-{q['id']}"
                    ).last()
                    answer = Answer.objects.filter(
                        question_id=question.id,
                        school__muni__id=muni.id,
                        answer__in=[question.get_option(qu) for qu in q["answer"]],
                    )
                    if not answer:
                        continue
                    _sum = []
                    for a in answer:
                        if a.answer in question.get_all_options():
                            _sum.append(a.frequency)
                    res = sum(_sum)
                    result = res / student_count
                    total_sum.append(round(result * 100, 2))
                if total_sum:
                    total_res = round((sum(total_sum) / len(total_sum)), 2)
                else:
                    total_res = 0
                if total_res < factor["prevalence_min"][0]:
                    prevalence = 0
                    recomendation = "Enhorabuena, con esta conducta de riesgo tu plantel no tiene un problema."
                elif (
                    total_res >= factor["prevalence_min"][0]
                    and total_res < factor["prevalence_min"][1]
                ):
                    prevalence = 1
                    recomendation = factor["recomendation_min"]
                    if hasattr(factor, "prevalence_mid"):
                        social_health_score = social_health_score + 1
                elif (
                    total_res >= factor["prevalence_mid"][0]
                    and total_res < factor["prevalence_mid"][1]
                ):
                    prevalence = 2
                    recomendation = factor["recomendation_mid"]
                    social_health_score = social_health_score + 1
                elif (
                    total_res >= factor["prevalence_high"][0]
                    and total_res < factor["prevalence_high"][1]
                ):
                    prevalence = 3
                    recomendation = factor["recomendation_high"]
                    social_health_score = social_health_score + 1
                elif "prevalence_veryhigh" in factor:
                    if (
                        total_res >= factor["prevalence_veryhigh"][0]
                        and total_res <= factor["prevalence_veryhigh"][1]
                    ):
                        prevalence = 4
                        recomendation = factor["recomendation_veryhigh"]
                        social_health_score = social_health_score + 1
                else:
                    prevalence = 3
                    recomendation = factor["recomendation_high"]
                    social_health_score = social_health_score + 1
                data_students[level].append(
                    {
                        "factor": factor["name"],
                        "value": total_res,
                        "recomendation": recomendation,
                        "count": round(student_count * (total_res / 100)),
                        "prevalence": prevalence,
                    }
                )

        # print(social_health_score)
        dic["social_health_stud"] = 1000 - (77 * social_health_score)
        dic["social_health_teach"] = 1000 - (77 * social_health_score_t)
        if school.emstype == 0:
            t = "A-SEC"
            age = school.answers.filter(question__key=f"{t}-1")
            gender = school.answers.filter(question__key=f"{t}-2")
            work = school.answers.filter(question__key=f"{t}-5")
            home = school.answers.filter(question__key=f"{t}-6")
        elif school.emstype == 1:
            t = "A-EMS"
            age = school.answers.filter(question__key=f"{t}-1")
            gender = school.answers.filter(question__key=f"{t}-2")
            work = school.answers.filter(question__key=f"{t}-5")
            home = school.answers.filter(question__key=f"{t}-6")
        elif school.emstype == 2:
            t = "A-ES"
            age = school.answers.filter(question__key=f"{t}-1")
            gender = school.answers.filter(question__key=f"{t}-2")
            work = school.answers.filter(question__key=f"{t}-5")
            home = school.answers.filter(question__key=f"{t}-6")
        dic["gender_data_stud"] = [
            {"frequency": answer.frequency, "name": answer.answer}
            for answer in gender.order_by("question__key")
        ]
        dic["age_data_stud"] = [
            {"frequency": answer.frequency, "name": answer.answer}
            for answer in age.order_by("question__key")
        ]
        dic["work_data_stud"] = [
            {"frequency": answer.frequency, "name": answer.answer}
            for answer in work.order_by("question__key")
        ]
        dic["home_data_stud"] = [
            {"frequency": answer.frequency, "name": answer.answer}
            for answer in home.order_by("question__key")
        ]
        if school.emstype == 0:
            t = "M-SEC"
            age = school.answers.filter(question__key=f"{t}-1")
            gender = school.answers.filter(question__key=f"{t}-2")
            work = school.answers.filter(question__key=f"{t}-5")
            home = school.answers.filter(question__key=f"{t}-6")
        elif school.emstype == 1:
            t = "D-EMS"
            age = school.answers.filter(question__key=f"{t}-1")
            gender = school.answers.filter(question__key=f"{t}-2")
            work = school.answers.filter(question__key=f"{t}-5")
            home = school.answers.filter(question__key=f"{t}-6")
        elif school.emstype == 2:
            t = "D-ES"
            age = school.answers.filter(question__key=f"{t}-1")
            gender = school.answers.filter(question__key=f"{t}-2")
            work = school.answers.filter(question__key=f"{t}-5")
            home = school.answers.filter(question__key=f"{t}-6")
        dic["gender_data_teach"] = [
            {"frequency": answer.frequency, "name": answer.answer}
            for answer in gender.order_by("question__key")
        ]
        dic["age_data_teach"] = [
            {"frequency": answer.frequency, "name": answer.answer}
            for answer in age.order_by("question__key")
        ]
        dic["work_data_teach"] = [
            {"frequency": answer.frequency, "name": answer.answer}
            for answer in work.order_by("question__key")
        ]
        dic["home_data_teach"] = [
            {"frequency": answer.frequency, "name": answer.answer}
            for answer in home.order_by("question__key")
        ]
        dic["data_teachers"] = data_teachers
        dic["data_students"] = data_students
        data.append(dic)
    ctx["data"] = data
    # task=pdf_reports_task.delay(request.user.id,ctx,'cedula_plantel')
    # if not os.path.exists(f"{settings.BASE_DIR}/media/tmp/{request.user.id}/cedula_plantel.pdf"):
    #     return JsonResponse({"status":"error"},status=400)
    # return JsonResponse({"status":'PDF generado con éxito',"pdf":_public_url(f"/media/tmp/{request.user.id}/cedula_plantel.pdf")})
    return {"status": "ok", "info": ctx}


def get_pdf(request):
    user = request.user.userapp
    schools = School.objects.none()
    try:
        if user.school:
            schools = School.objects.filter(id=user.school.id)
    except:
        pass
    try:
        if user.subsystem:
            schools = School.objects.filter(subsystem__id=user.subsystem.id)
    except:
        pass
    try:
        if user.level:
            if user.level in [0, 1, 2]:
                schools = School.objects.filter(emstype=user.level)
            if user.level == 3:
                schools = School.objects.filter(muni__id=user.municipality.id)
    except:
        pass
    info = [
        get_data(student_teacher=True, is_school=True, school=item) for item in schools
    ]
    task = pdf_reports_task.delay(
        request.user.id, {"data": info}, template="cedula_plantel"
    )
    # if not os.path.exists(f"{settings.BASE_DIR}/media/tmp/{request.user.id}/cedula.pdf"):
    #     return JsonResponse({"status":"error"},status=400)
    # "data":info,
    return JsonResponse(
        {
            "status": "PDF generado con éxito",
            "pdf": _public_url(f"/media/tmp/{request.user.id}/cedula.pdf"),
        }
    )


def get_pdff(request):
    ctx = {}
    return render(request, "cedulaf.html")
