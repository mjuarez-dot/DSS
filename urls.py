"""dssh URL Configuration

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/3.0/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import path
from app.db.models import *
from app.api.views import *
from django.conf import settings
from django.conf.urls.static import static

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', IndexView.as_view(), name='index'),
    path('login', LoginView.as_view(), name='login'),
    path('logout/<int:logout>/', LoginView.as_view(), name='logout'),
    path('main', MainView.as_view(), name='main'),
    path('survey_form', SurveyFormView.as_view(), name='survey_form'),
    path('import_answers', import_answers, name='import_answers'),
    path('get_questions', get_questions, name='get_questions'),
    path('get_schools', get_schools, name='get_schools'),
    path('get_muni', get_muni, name='get_muni'),
    path('get_subsystem', get_subsystem, name='get_subsystem'),
    path('get_map_data', get_map_data, name='get_map_data'),
    path('change_role', change_role, name='change_role'),
    path('get_chart_data', get_chart_data, name='get_chart_data'),
    path('get_student_file', get_student_file, name='get_student_file'),
    path('get_teacher_file', get_teacher_file, name='get_teacher_file'),
    path('intro', IntroView.as_view(), name='intro'),
    path('map', MapView.as_view(), name='map'),
    path('glossary', GlossaryView.as_view(), name='glossary'),
    path('report', ReportView.as_view(), name='report'),
    path('report/<int:teachers>', ReportView.as_view(), name='report'),
    path('report/<int:teachers>/<str:level>', ReportView.as_view(), name='report'),
    path('get_pdf', get_pdf, name='get_pdf'),
    path('get_pdf_school', get_pdf_school, name='get_pdf_school'),
    path('get_pdff', get_pdff, name='get_pdff'),
]

urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)
