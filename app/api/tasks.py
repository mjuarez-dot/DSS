    
from app.db.models import *
import time, logging
from django.conf import settings
from django.template.loader import render_to_string
import os,logging

try:
    from celery import shared_task
except ImportError:
    def shared_task(func):
        return func
p_= logging.getLogger(__name__)
logger = logging.getLogger('weasyprint')
logger.handlers = [] 
# @shared_task
# def save_excel_data(excel_file):
#     p_.error("start")
#     wb = openpyxl.load_workbook(filename=excel_file,data_only=True)

#     # getting a particular sheet by name out of many sheets
#     p_.error(wb)
@shared_task
def pdf_reports_task(user,ctx,template='cedula'):
    try:
        from weasyprint import HTML
    except ImportError as exc:
        raise RuntimeError("weasyprint is required to generate PDF reports") from exc

    # p_.error(ctx)
    html_string=render_to_string(f'{template}.html', ctx)
    html=HTML(string=html_string)
    if not os.path.exists(f"{settings.BASE_DIR}/media"):
        os.mkdir(f"{settings.BASE_DIR}/media")
    if not os.path.exists(f"{settings.BASE_DIR}/media/tmp"):
        os.mkdir(f"{settings.BASE_DIR}/media/tmp")
    if not os.path.exists(f"{settings.BASE_DIR}/media/tmp/{user}"):
        os.mkdir(f"{settings.BASE_DIR}/media/tmp/{user}")

    p_.error(html)
    target=f'{settings.BASE_DIR}/media/tmp/{user}/{template}.pdf'
    pdf=html.write_pdf(stylesheets=[
        settings.BASE_DIR+ "/static/assets/css/fontawesome.css",
        settings.BASE_DIR+ "/static/assets/css/icofont.css",
        settings.BASE_DIR+ "/static/assets/css/themify.css",
        settings.BASE_DIR+ "/static/assets/css/flag-icon.css",
        settings.BASE_DIR+ "/static/assets/css/feather-icon.css",
        settings.BASE_DIR+ "/static/assets/css/prism.css",
        settings.BASE_DIR+ "/static/assets/css/photoswipe.css",
        settings.BASE_DIR+ "/static/assets/css/bootstrap.css",
        settings.BASE_DIR+ "/static/assets/css/style.css",
        settings.BASE_DIR+ "/static/assets/css/color-1.css",
        settings.BASE_DIR+ "/static/assets/css/responsive.css",
    ]);
    f = open(target, 'wb')
    f.write(pdf)
    p_.error(f"{settings.BASE_DIR}/media/tmp/{user}/{template}.pdf")
    p_.error(os.path.exists(f"{settings.BASE_DIR}/media/tmp/{user}/{template}.pdf"))

@shared_task
def save_excel_data(excel_file):
    try:
        import openpyxl
    except ImportError as exc:
        raise RuntimeError("openpyxl is required to import Excel answers") from exc

    p_.error("start")
    wb = openpyxl.load_workbook(filename=excel_file,data_only=True)

    # getting a particular sheet by name out of many sheets
    p_.error(wb)
    worksheet = wb.active
    p_.error(worksheet)
    #get headers
    schools={}
    # p_.error(time.process_time())

    for col in worksheet.iter_cols(max_row=1):
        for cell in col:
            if cell.value=="ID_PLANTEL":
                for row in worksheet.iter_rows(
                    min_col=cell.column,max_col=cell.column,min_row=2):
                    val=row[0].value
                    if val in schools:
                        if row[0].row not in schools[val]:
                            schools[val].append(row[0].row)
                    else:
                        schools[val]=[row[0].row]
                break
    # p_.error(time.process_time())
    for col in worksheet.iter_cols(min_col=4,max_row=1):
        results={}
        for cell in col:
            # print(cell.value)
            splitted_cell=cell.value.split('-')
            try:
                question_id=f"{splitted_cell[0]}-{splitted_cell[1]}"
                # surveys=[sur.survey_name for sur in Survey.objects.all().only('survey_name') if sur.survey_name.startswith(question_id)]
            except:
                question_id=None
                # surveys=[]
            # if len(surveys)>0:
            if Survey.objects.filter(survey_name__startswith=question_id).exists():
                # pass
                for school_id,rows in schools.items():
                    # print(f"{school_id}")
                    # get first and last row num
                    #find school in results
                    # if school_id in results:
                    # get values of first question 
                    values=[row[0] for row in worksheet.iter_rows(
                        min_col=cell.column,max_col=cell.column,
                        min_row=rows[0],max_row=rows[-1],
                        values_only=True
                    ) if row[0]]
                    # worksheet[f"{cell.coordinate}"]
                    sch=School.objects.filter(id=school_id).last()
                    qst=Question.objects.filter(key=cell.value).last()
                    def update_answer(school, group,question,answer,frequency):
                        defa={
                                "school":school,
                                "group":group,
                                "question":question,
                                "answer":answer,
                                "frequency":frequency
                            }
                        answer=Answer.objects.filter(school=school, group=group,question=question,answer=answer)
                        if not answer.exists():
                            new=Answer.objects.create(**defa)
                        else:
                            new=answer.update(**defa)
                    if qst.option_a:
                        update_answer(school=sch, group=None,question=qst,answer=qst.option_a,frequency=values.count('A'))
                    if qst.option_b:
                        update_answer(school=sch, group=None,question=qst,answer=qst.option_b,frequency=values.count('B'))
                    if qst.option_c:
                        update_answer(school=sch, group=None,question=qst,answer=qst.option_c,frequency=values.count('C'))
                    if qst.option_d:
                        update_answer(school=sch, group=None,question=qst,answer=qst.option_d,frequency=values.count('D'))
                    if qst.option_e:
                        update_answer(school=sch, group=None,question=qst,answer=qst.option_e,frequency=values.count('E'))
                p_.error(cell.value)
            else: 
                continue
            
