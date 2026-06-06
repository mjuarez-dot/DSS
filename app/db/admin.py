from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.utils.translation import gettext, gettext_lazy as _
from django.contrib.auth.models import User, Group
from django.contrib.admin import SimpleListFilter
from .models import *
from datetime import datetime

try:
    from import_export.admin import ImportExportModelAdmin
    from import_export.widgets import ForeignKeyWidget
    from import_export import resources, fields
    HAS_IMPORT_EXPORT = True
except ImportError:
    ImportExportModelAdmin = object
    HAS_IMPORT_EXPORT = False

    class _FallbackResource:
        pass

    class _FallbackResources:
        ModelResource = _FallbackResource

    class _FallbackField:
        def __init__(self, *args, **kwargs):
            pass

    class ForeignKeyWidget:
        def __init__(self, *args, **kwargs):
            pass

    resources = _FallbackResources()
    fields = type("fields", (), {"Field": _FallbackField})

BaseAdmin = ImportExportModelAdmin if HAS_IMPORT_EXPORT else admin.ModelAdmin

class MunicipalityResource(resources.ModelResource):
    id=fields.Field(attribute='id', column_name='id')
    key=fields.Field(attribute='key', column_name='codigo')
    name=fields.Field(attribute='name', column_name='nombre')
    population=fields.Field(attribute='population', column_name='poblacion')
    state=fields.Field(attribute='state', column_name='estado',
        widget=ForeignKeyWidget(State, 'id'))
    # inspection=fields.Field(attribute='inspection', column_name='Inspecciones_asociados')
    class Meta:
        model = Municipality
        fields=('id','key','name','population','state')

class UserResource(resources.ModelResource):
    username=fields.Field(attribute='username', column_name='clave_del_plantel')
    first_name=fields.Field(attribute='first_name', column_name='contraseña')
    # inspection=fields.Field(attribute='inspection', column_name='Inspecciones_asociados')
    class Meta:
        model = Municipality
        fields=('username','first_name',)
    # def dehydrate_state(self,instance):
    #     return f"{instance.state.name}"

class SchoolResource(resources.ModelResource):
    id=fields.Field(attribute='id', column_name='id')
    muni=fields.Field(attribute='muni', column_name='municipio',widget=ForeignKeyWidget(Municipality, 'id'))
    subsystem=fields.Field(attribute='subsystem', column_name='subsistema',widget=ForeignKeyWidget(Subsystem, 'id'))
    school_name=fields.Field(attribute='school_name', column_name='nombre_del_plantel')
    school_key=fields.Field(attribute='school_key', column_name='clave_del_plantel')
    student_num=fields.Field(attribute='student_num', column_name='numero_estudiantes')
    teacher_num=fields.Field(attribute='teacher_num', column_name='numero_docentes')
    emstype=fields.Field(attribute='emstype', column_name='tipo_subsistema')
    address=fields.Field(attribute='address', column_name='domicilio')
    terminal_efficiency=fields.Field(attribute='terminal_efficiency', column_name='eficiencia_terminal')
    reprobation=fields.Field(attribute='reprobation', column_name='reprobacion')
    # inspection=fields.Field(attribute='inspection', column_name='Inspecciones_asociados')
    class Meta:
        model = School
        fields=("id","muni","subsystem","school_name","school_key","student_num","teacher_num","emstype",)
    # def dehydrate_emstype(self,instance):
    #     return instance.get_emstype_display()

class GroupResource(resources.ModelResource):
    id=fields.Field(attribute='id', column_name='id')
    school=fields.Field(attribute='school', column_name='plantel',widget=ForeignKeyWidget(School, 'id'))
    shift=fields.Field(attribute='shift', column_name='turno')
    semester=fields.Field(attribute='semester', column_name='semestre')
    group=fields.Field(attribute='group', column_name='nombre_grupo')
    student_num=fields.Field(attribute='student_num', column_name='numero_estudiantes')
    init_num=fields.Field(attribute='init_num', column_name='folio_inicial')
    end_num=fields.Field(attribute='end_num', column_name='folio_final')
    # inspection=fields.Field(attribute='inspection', column_name='Inspecciones_asociados')
    class Meta:
        model = Group
        fields=("id","school","shift","semester","group","student_num","init_num","end_num",)

class QuestionResource(resources.ModelResource):
    id=fields.Field(attribute='id', column_name='id')
    survey=fields.Field(attribute='survey', column_name='encuesta',widget=ForeignKeyWidget(Survey, 'id'))
    key=fields.Field(attribute='key', column_name='clave')
    question=fields.Field(attribute='question', column_name='pregunta')
    option_a=fields.Field(attribute='option_a', column_name='opcion_a')
    option_b=fields.Field(attribute='option_b', column_name='opcion_b')
    option_c=fields.Field(attribute='option_c', column_name='opcion_c')
    option_d=fields.Field(attribute='option_d', column_name='opcion_d')
    option_e=fields.Field(attribute='option_e', column_name='opcion_e')
    class Meta:
        model = Question
        fields=("id","survey","key","question","option_a","option_b","option_c","option_d","option_e",)


class AnswerResource(resources.ModelResource):
    school=fields.Field(attribute='school', column_name='plantel',
        widget=ForeignKeyWidget(State, 'id'))
    group=fields.Field(attribute='group', column_name='grupo',
        widget=ForeignKeyWidget(State, 'id'))
    question=fields.Field(attribute='question', column_name='pregunta',
        widget=ForeignKeyWidget(State, 'id'))
    answer=fields.Field(attribute='answer', column_name='respuesta')
    frequency=fields.Field(attribute='frequency', column_name='frequencia')
    id=fields.Field(attribute='id', column_name='id')
    # inspection=fields.Field(attribute='inspection', column_name='Inspecciones_asociados')
    class Meta:
        model = Answer
        fields=("id","school","group","question","answer","frequency",)
    # def dehydrate_state(self,instance):
    #     return f"{instance.state.name}"

admin.site.unregister(User)

class User_Inline(admin.StackedInline):
    model = UserApp
    can_delete = False
    verbose_name = 'Configuraciones'
    verbose_name_plural = 'Configuraciones adicionales'
if HAS_IMPORT_EXPORT:
    class UserAdmin(ImportExportModelAdmin, DjangoUserAdmin):
        fieldsets = (
            (None, {'fields': ('username', 'password')}),
            (_('Personal info'), {
                'fields': ('first_name', 'last_name', 'email')}),
            (_('Permissions'), {
                'fields': ('is_active', 'is_staff', 'is_superuser')}),
            (_('Important dates'), {
                'fields': ('last_login', 'date_joined')}),
        )
        inlines = (User_Inline,)
        resource_class = UserResource
else:
    class UserAdmin(DjangoUserAdmin):
        fieldsets = (
            (None, {'fields': ('username', 'password')}),
            (_('Personal info'), {
                'fields': ('first_name', 'last_name', 'email')}),
            (_('Permissions'), {
                'fields': ('is_active', 'is_staff', 'is_superuser')}),
            (_('Important dates'), {
                'fields': ('last_login', 'date_joined')}),
        )
        inlines = (User_Inline,)
admin.site.register(User, UserAdmin)
admin.site.register(State)


class MunicipalityAdmin(BaseAdmin):
    search_fields = ['key','name','state__name']
    list_display = ('key','name','state')
    ordering = ['-id']
    resource_class=MunicipalityResource
admin.site.register(Municipality,MunicipalityAdmin)

class SchoolAdmin(BaseAdmin):
    search_fields = ['id','school_name','emstype']
    list_display = ('id','school_key','school_name','muni','subsystem',)
    ordering = ['-id']
    resource_class=SchoolResource
admin.site.register(School,SchoolAdmin)


admin.site.register(Subsystem)

class GroupAdmin(BaseAdmin):
    search_fields = ['id','school_name','group',]
    list_display = ('id','school','shift','semester','group')
    ordering = ['-id']
    resource_class=GroupResource
admin.site.register(Group,GroupAdmin)

admin.site.register(Survey)

class QuestionAdmin(BaseAdmin):
    search_fields = ['id','survey__name','question','key',]
    list_display = ('id','key','question',"option_a","option_b","option_c","option_d","option_e",)
    ordering = ['-id']
    resource_class=QuestionResource
admin.site.register(Question,QuestionAdmin)

class AnswerAdmin(BaseAdmin):
    search_fields = ['school__name','question__question']
    list_filter=('school__school_name','school__emstype',)
    list_display = ("school","group","question","answer","frequency",)
    ordering = ['-id']
    resource_class=AnswerResource
admin.site.register(Answer,AnswerAdmin)
admin.site.register(ReportResult)
# class PlayerClassAdmin(admin.ModelAdmin):
#     search_fields = ['id']
#     search_filter=('main_class','sub_class','class_type')
#     list_display = ('id','class_name','class_type')
#     ordering = ['id']
#     def class_name(self,obj):
#         return "{}-{}".format(obj.get_main_class_display(),obj.get_sub_class_display())
#     def class_type(self,obj):
#         return obj.get_sub_class_display()
# admin.site.register(PlayerClass,PlayerClassAdmin)
# class RaidAdmin(admin.ModelAdmin):
#     search_fields = ['id','name']
#     search_filter=('stage',)
#     list_display = ('id','name','stage','min_ilvl','date','player_num')
#     ordering = ['id']
# admin.site.register(Raid,RaidAdmin)
# class PlayerAdmin(admin.ModelAdmin):
#     search_fields = ['id','name','discord']
#     search_filter=('player_class',)
#     list_display = ('id','name','discord','ilvl','sub_class')
#     ordering = ['id']
#     def sub_class(self,obj):
#         return obj.player_class.get_sub_class_display()
# admin.site.register(Player,PlayerAdmin)
# class isActiveFilter(SimpleListFilter):
#     title = 'Raids Activos' # or use _('country') for translated title
#     parameter_name = 'active_raid'
#     def lookups(self, request, model_admin):
#         return (
#             ('active', _('Raid activo')),
#             ('inactive', _('Raid inactivo')),
#             )
#     def queryset(self, request, queryset):
#         if self.value() == 'active':
#             return queryset.filter(date_lte=datetime.now())
#         if self.value() == 'inactive':
#             return queryset.filter(date_gt=datetime.now())
# class PlayerRaidAdmin(admin.ModelAdmin):
#     search_fields = ['id','raid__name','player__discord','player__name']
#     search_filter=('player__player_class','raid__stage','party',isActiveFilter)
#     list_display = ('id','raid','player','party_number','sub_class')
#     ordering = ['id']
    
#     def sub_class(self,obj):
#         return obj.player.player_class.get_sub_class_display()
# admin.site.register(PlayerRaid,PlayerRaidAdmin)
