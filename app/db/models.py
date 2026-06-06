# -*- coding: utf-8 -*-
from django.db import models
from django.contrib.auth.models import User

try:
    JSONField = models.JSONField
except AttributeError:
    from django.contrib.postgres.fields import JSONField
class UserApp(models.Model):
    user = models.OneToOneField(User,related_name='userapp',on_delete=models.CASCADE)
    school=models.ForeignKey('School',verbose_name="Plantel",blank=True,null=True,on_delete=models.CASCADE)
    subsystem=models.ForeignKey('Subsystem',verbose_name="Subsistema",blank=True,null=True,on_delete=models.CASCADE)
    municipality=models.ForeignKey('Municipality',verbose_name="Municipio",blank=True,null=True,on_delete=models.CASCADE)
    level=models.IntegerField('Nivel',choices=enumerate([
        "Secundaria",
        "Media Superior",
        "Superior",
        "Municipio",
        "Estado"
    ]),blank=True,null=True)
    class Meta:
        verbose_name=u'OPCION'
        verbose_name_plural=u'OPCIONES'

    def __str__(self):
        return "{} {}".format(self.user.first_name,self.user.last_name)

class State(models.Model):
    name = models.CharField('Nombre', max_length=100)
    def __str__(self):
        return "{}".format(self.name)

class Municipality(models.Model):
    key = models.CharField('Clave', max_length=10)
    name = models.CharField('Nombre', max_length=100)
    population = models.PositiveIntegerField('Poblacion')
    state=models.ForeignKey(State,verbose_name="Estado", on_delete=models.CASCADE)
    def __str__(self):
        return "{}".format(self.name)

class Subsystem(models.Model):
    abrev = models.CharField('Abreviacion', max_length=10)
    name = models.CharField('Nombre del Subsistema', max_length=100)
    
    def __str__(self):
        return "{}".format(self.name)

class School(models.Model):
    muni=models.ForeignKey(Municipality,verbose_name="Municipio", on_delete=models.CASCADE)
    subsystem=models.ForeignKey(Subsystem,related_name="schools", verbose_name="Subsistema", on_delete=models.CASCADE)
    school_key= models.CharField('Clave del Plantel', max_length=50)
    school_name= models.CharField('Nombre del Plantel', max_length=200)
    student_num= models.IntegerField('Número de alumnos')
    teacher_num= models.IntegerField('Número de docentes')
    emstype=models.IntegerField('Tipo de subsistema',choices=enumerate([
        "Secundaria",
        "Media Superior",
        "Superior",
    ]))
    address=models.CharField('Domicilio', max_length=250, default='')
    terminal_efficiency=models.FloatField('Eficiencia terminal', default=0)
    reprobation=models.FloatField('Reprobacion', default=0)
    def __str__(self):
        return "{}".format(self.school_name)

class Group(models.Model):
    school=models.ForeignKey(School,verbose_name="Plantel", on_delete=models.CASCADE)
    shift= models.CharField('Turno', max_length=10)
    semester= models.CharField('Semestre', max_length=10)
    group= models.CharField('Grupo', max_length=80)
    student_num= models.IntegerField('Número de alumnos')
    init_num= models.IntegerField('Folio inicial')
    end_num= models.IntegerField('Folio final')
    def __str__(self):
        return "{}".format(self.group)

class Survey(models.Model):
    survey_name= models.CharField('Encuesta', max_length=30)
    number_1= models.CharField('Folio inicial', max_length=10)
    number_2= models.CharField('Folio final', max_length=10)
    type=models.IntegerField('Tipo',choices=enumerate([
        "Alumnos",
        "Docentes - No Docentes",
    ]))
    def __str__(self):
        return "{}".format(self.survey_name)

class Question(models.Model):
    survey= models.ForeignKey(Survey,verbose_name="Encuesta", on_delete=models.CASCADE)
    key= models.CharField('Clave', max_length=10)
    question= models.CharField('Pregunta', max_length=250)
    option_a= models.CharField('Opción A', max_length=100,blank=True,null=True)
    option_b= models.CharField('Opción B', max_length=100,blank=True,null=True)
    option_c= models.CharField('Opción C', max_length=100,blank=True,null=True)
    option_d= models.CharField('Opción D', max_length=100,blank=True,null=True)
    option_e= models.CharField('Opción E', max_length=100,blank=True,null=True)
    def get_option(self,op):
        if op=='A':
            option=self.option_a
        elif op=='B':
            option=self.option_b
        elif op=='C':
            option=self.option_c
        elif op=='D':
            option=self.option_d
        elif op=='E':
            option=self.option_e
        return option
    def get_all_options(self):
        lst=[]
        if self.option_a:
            lst.append(self.option_a)
        if self.option_b:
            lst.append(self.option_b)
        if self.option_c:
            lst.append(self.option_c)
        if self.option_d:
            lst.append(self.option_d)
        if self.option_e:
            lst.append(self.option_e)
        return lst
    def __str__(self):
        return "{}-{}".format(self.key,self.question)

class Answer(models.Model):
    school=models.ForeignKey(School,related_name='answers',verbose_name="Plantel", on_delete=models.CASCADE)
    group=models.ForeignKey(Group,verbose_name="Grupo", on_delete=models.CASCADE,null=True,blank=True)
    question= models.ForeignKey(Question,verbose_name="Pregunta", on_delete=models.CASCADE)
    answer=models.CharField('Respuesta', max_length=150)
    frequency=models.IntegerField('Frequencia',default=0)
    def __str__(self):
        return "{} - {}".format(self.question.question,self.answer)

class ReportResult(models.Model):
    student_teacher=models.BooleanField('Estudiante/Maestro(pdf)',null=True,blank=True)
    is_teacher=models.BooleanField('Es Maestro',null=True,blank=True) 
    is_school=models.BooleanField('Es Escuela',null=True,blank=True)
    is_subsystem=models.BooleanField('Es subsistema',null=True,blank=True)
    is_system=models.BooleanField('Es sistema',null=True,blank=True)
    is_muni=models.BooleanField('Es municipio',null=True,blank=True)
    subsystem=models.ForeignKey(Subsystem, verbose_name="Subsistema", on_delete=models.CASCADE ,null=True,blank=True)
    # if system==0:t='M-SEC'
    # if system==1:t='D-EMS'
    # if system==2:t='D-ES'
    system=models.IntegerField('Sistema (0,1,2)',null=True,blank=True)
    system_name=models.CharField('Nombre del sistema', max_length=150,null=True,blank=True)
    school=models.ForeignKey(School,verbose_name="Plantel", on_delete=models.CASCADE,null=True,blank=True)
    muni=models.ForeignKey(Municipality,verbose_name="Municipio", on_delete=models.CASCADE,null=True,blank=True)
    simple=models.BooleanField('Simple', null=True,blank=True)
    level=models.CharField('Nivel', max_length=150,null=True,blank=True)
    result = JSONField(u'Resultado en json', blank=True, null=True)
