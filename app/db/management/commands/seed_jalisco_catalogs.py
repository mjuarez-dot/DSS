from django.core.management.base import BaseCommand

from app.db.models import Municipality, State, Subsystem


JALISCO_MUNICIPALITIES = [
    ("14-001", "Acatic"),
    ("14-002", "Acatlan de Juarez"),
    ("14-003", "Ahualulco de Mercado"),
    ("14-004", "Amacueca"),
    ("14-005", "Amatitan"),
    ("14-006", "Ameca"),
    ("14-007", "San Juanito de Escobedo"),
    ("14-008", "Arandas"),
    ("14-009", "El Arenal"),
    ("14-010", "Atemajac de Brizuela"),
    ("14-011", "Atengo"),
    ("14-012", "Atenguillo"),
    ("14-013", "Atotonilco el Alto"),
    ("14-014", "Atoyac"),
    ("14-015", "Autlan de Navarro"),
    ("14-016", "Ayotlan"),
    ("14-017", "Ayutla"),
    ("14-018", "La Barca"),
    ("14-019", "Bolanos"),
    ("14-020", "Cabo Corrientes"),
    ("14-021", "Casimiro Castillo"),
    ("14-022", "Cihuatlan"),
    ("14-023", "Zapotlan El Grande"),
    ("14-024", "Cocula"),
    ("14-025", "Colotlan"),
    ("14-026", "Concepcion de Buenos Aires"),
    ("14-027", "Cuautitlan de Garcia Barragan"),
    ("14-028", "Cuautla"),
    ("14-029", "Cuquio"),
    ("14-030", "Chapala"),
    ("14-031", "Chimaltitan"),
    ("14-032", "Chiquilistlan"),
    ("14-033", "Degollado"),
    ("14-034", "Ejutla"),
    ("14-035", "Encarnacion de Diaz"),
    ("14-036", "Etzatlan"),
    ("14-037", "El Grullo"),
    ("14-038", "Guachinango"),
    ("14-039", "Guadalajara"),
    ("14-040", "Hostotipaquillo"),
    ("14-041", "Huejucar"),
    ("14-042", "Huejuquilla el Alto"),
    ("14-043", "La Huerta"),
    ("14-044", "Ixtlahuacan de los Membrillos"),
    ("14-045", "Ixtlahuacan del Rio"),
    ("14-046", "Jalostotitlan"),
    ("14-047", "Jamay"),
    ("14-048", "Jesus Maria"),
    ("14-049", "Jilotlan de los Dolores"),
    ("14-050", "Jocotepec"),
    ("14-051", "Juanacatlan"),
    ("14-052", "Juchitlan"),
    ("14-053", "Lagos de Moreno"),
    ("14-054", "El Limon"),
    ("14-055", "Magdalena"),
    ("14-056", "Santa Maria del Oro"),
    ("14-057", "La Manzanilla de la Paz"),
    ("14-058", "Mascota"),
    ("14-059", "Mazamitla"),
    ("14-060", "Mexticacan"),
    ("14-061", "Mezquitic"),
    ("14-062", "Mixtlan"),
    ("14-063", "Ocotlan"),
    ("14-064", "Ojuelos de Jalisco"),
    ("14-065", "Pihuamo"),
    ("14-066", "Poncitlan"),
    ("14-067", "Puerto Vallarta"),
    ("14-068", "Villa Purificacion"),
    ("14-069", "Quitupan"),
    ("14-070", "El Salto"),
    ("14-071", "San Cristobal de la Barranca"),
    ("14-072", "San Diego de Alejandria"),
    ("14-073", "San Juan de los Lagos"),
    ("14-074", "San Julian"),
    ("14-075", "San Marcos"),
    ("14-076", "San Martin de Bolanos"),
    ("14-077", "San Martin Hidalgo"),
    ("14-078", "San Miguel el Alto"),
    ("14-079", "Gomez Farias"),
    ("14-080", "San Sebastian del Oeste"),
    ("14-081", "Santa Maria de los Angeles"),
    ("14-082", "Sayula"),
    ("14-083", "Tala"),
    ("14-084", "Talpa de Allende"),
    ("14-085", "Tamazula de Gordiano"),
    ("14-086", "Tapalpa"),
    ("14-087", "Tecalitlan"),
    ("14-088", "Tecolotlan"),
    ("14-089", "Techaluta de Montenegro"),
    ("14-090", "Tenamaxtlan"),
    ("14-091", "Teocaltiche"),
    ("14-092", "Teocuitatlan de Corona"),
    ("14-093", "Tepatitlan de Morelos"),
    ("14-094", "Tequila"),
    ("14-095", "Teuchitlan"),
    ("14-096", "Tizapan el Alto"),
    ("14-097", "Tlajomulco de Zuniga"),
    ("14-098", "San Pedro Tlaquepaque"),
    ("14-099", "Toliman"),
    ("14-100", "Tomatlan"),
    ("14-101", "Tonala"),
    ("14-102", "Tonaya"),
    ("14-103", "Tonila"),
    ("14-104", "Totatiche"),
    ("14-105", "Tototlan"),
    ("14-106", "Tuxcacuesco"),
    ("14-107", "Tuxcueca"),
    ("14-108", "Tuxpan"),
    ("14-109", "Union de San Antonio"),
    ("14-110", "Union de Tula"),
    ("14-111", "Valle de Guadalupe"),
    ("14-112", "Valle de Juarez"),
    ("14-113", "San Gabriel"),
    ("14-114", "Villa Corona"),
    ("14-115", "Villa Guerrero"),
    ("14-116", "Villa Hidalgo"),
    ("14-117", "Canadas de Obregon"),
    ("14-118", "Yahualica de Gonzalez Gallo"),
    ("14-119", "Zacoalco de Torres"),
    ("14-120", "Zapopan"),
    ("14-121", "Zapotiltic"),
    ("14-122", "Zapotitlan de Vadillo"),
    ("14-123", "Zapotlan del Rey"),
    ("14-124", "Zapotlanejo"),
    ("14-125", "San Ignacio Cerro Gordo"),
]

SUBSYSTEMS = [
    ("SEC-GRAL", "Secundaria - General"),
    ("SEC-TEC", "Secundaria - Tecnica"),
    ("SEC-TELE", "Secundaria - Telesecundaria"),
    ("SEC-COM", "Secundaria - Comunitaria"),
    ("SEC-TRAB", "Secundaria - Para Trabajadores"),
    ("SEC-ADUL", "Secundaria - Educacion para Adultos"),
    ("EMS-BG", "Media Superior - Bachillerato General"),
    ("EMS-BT", "Media Superior - Bachillerato Tecnologico"),
    ("EMS-CONA", "Media Superior - CONALEP"),
    ("EMS-TELE", "Media Superior - Telebachillerato"),
    ("EMS-EMSAD", "Media Superior - EMSaD"),
    ("EMS-VIRT", "Media Superior - Bachillerato Virtual"),
    ("ES-UPF", "Superior - Universidades Publicas Federales"),
    ("ES-UE", "Superior - Universidades Estatales"),
    ("ES-UT", "Superior - Universidades Tecnologicas"),
    ("ES-UP", "Superior - Universidades Politecnicas"),
    ("ES-TECNM", "Superior - TecNM"),
    ("ES-NORM", "Superior - Normales"),
    ("ES-INTER", "Superior - Interculturales"),
    ("ES-CPI", "Superior - Centros Publicos de Investigacion"),
    ("ES-PRIV", "Superior - Universidades Privadas"),
]


class Command(BaseCommand):
    help = "Seed Jalisco municipalities and educational subsystems."

    def handle(self, *args, **options):
        state, state_created = State.objects.get_or_create(name="Jalisco")

        municipality_created = 0
        municipality_updated = 0
        for key, name in JALISCO_MUNICIPALITIES:
            municipality, created = Municipality.objects.update_or_create(
                key=key,
                defaults={
                    "name": name,
                    "population": 0,
                    "state": state,
                },
            )
            if created:
                municipality_created += 1
            else:
                changed = False
                if municipality.state_id != state.id:
                    municipality.state = state
                    changed = True
                if municipality.population is None:
                    municipality.population = 0
                    changed = True
                if changed:
                    municipality.save(update_fields=["state", "population"])
                municipality_updated += 1

        subsystem_created = 0
        subsystem_updated = 0
        for abrev, name in SUBSYSTEMS:
            _, created = Subsystem.objects.update_or_create(
                abrev=abrev,
                defaults={"name": name},
            )
            if created:
                subsystem_created += 1
            else:
                subsystem_updated += 1

        self.stdout.write(
            self.style.SUCCESS(
                (
                    f"Estado {'creado' if state_created else 'verificado'}: {state.name}. "
                    f"Municipios creados={municipality_created}, actualizados={municipality_updated}. "
                    f"Subsistemas creados={subsystem_created}, actualizados={subsystem_updated}."
                )
            )
        )
