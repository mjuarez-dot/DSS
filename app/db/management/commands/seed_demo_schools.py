from django.core.management.base import BaseCommand, CommandError

from app.db.models import Group, Municipality, School, State, Subsystem


GROUP_DEFINITIONS = [
    {
        "shift": "Matutino",
        "semester": "1",
        "group": "A",
        "student_num": 30,
        "init_num": 1000,
        "end_num": 1029,
    },
    {
        "shift": "Vespertino",
        "semester": "1",
        "group": "B",
        "student_num": 30,
        "init_num": 2000,
        "end_num": 2029,
    },
]


def emstype_from_subsystem(abrev):
    if abrev.startswith("SEC-"):
        return 0
    if abrev.startswith("EMS-"):
        return 1
    if abrev.startswith("ES-"):
        return 2
    raise ValueError(f"Unsupported subsystem abbreviation: {abrev}")


class Command(BaseCommand):
    help = "Create one demo school per subsystem and two demo groups per school."

    def handle(self, *args, **options):
        try:
            jalisco = State.objects.get(name="Jalisco")
        except State.DoesNotExist as exc:
            raise CommandError(
                "State 'Jalisco' does not exist. Run seed_jalisco_catalogs first."
            ) from exc

        municipalities = list(Municipality.objects.filter(state=jalisco).order_by("key"))
        subsystems = list(Subsystem.objects.order_by("abrev"))

        if not municipalities:
            raise CommandError(
                "No municipalities found for Jalisco. Run seed_jalisco_catalogs first."
            )
        if not subsystems:
            raise CommandError(
                "No subsystems found. Run seed_jalisco_catalogs first."
            )

        schools_created = 0
        schools_updated = 0
        groups_created = 0
        groups_updated = 0

        for index, subsystem in enumerate(subsystems, start=1):
            muni = municipalities[(index - 1) % len(municipalities)]
            emstype = emstype_from_subsystem(subsystem.abrev)
            school_key = f"DEMO-{subsystem.abrev}"
            school_name = f"Plantel Demo {subsystem.name}"

            school, created = School.objects.update_or_create(
                school_key=school_key,
                defaults={
                    "muni": muni,
                    "subsystem": subsystem,
                    "school_name": school_name,
                    "student_num": 60,
                    "teacher_num": 12,
                    "emstype": emstype,
                    "address": f"Domicilio conocido, {muni.name}, Jalisco",
                    "terminal_efficiency": 0,
                    "reprobation": 0,
                },
            )
            if created:
                schools_created += 1
            else:
                schools_updated += 1

            for group_def in GROUP_DEFINITIONS:
                _, group_created = Group.objects.update_or_create(
                    school=school,
                    shift=group_def["shift"],
                    group=group_def["group"],
                    defaults={
                        "semester": group_def["semester"],
                        "student_num": group_def["student_num"],
                        "init_num": group_def["init_num"],
                        "end_num": group_def["end_num"],
                    },
                )
                if group_created:
                    groups_created += 1
                else:
                    groups_updated += 1

        self.stdout.write(
            self.style.SUCCESS(
                (
                    f"Planteles demo creados={schools_created}, actualizados={schools_updated}. "
                    f"Grupos creados={groups_created}, actualizados={groups_updated}."
                )
            )
        )
