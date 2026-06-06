import re

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError

from app.db.models import Municipality, School, State, Subsystem, UserApp


DEFAULT_PASSWORD = "Demo123!"
MAX_USERNAME_LENGTH = 30
MAX_FIRST_NAME_LENGTH = 30


def normalize(value):
    value = value.lower().strip()
    value = re.sub(r"[^a-z0-9]+", "_", value)
    return re.sub(r"_+", "_", value).strip("_")


def username_with_prefix(prefix, value):
    normalized = normalize(value)
    username = f"{prefix}_{normalized}"
    if len(username) <= MAX_USERNAME_LENGTH:
        return username
    available = MAX_USERNAME_LENGTH - len(prefix) - 1
    return f"{prefix}_{normalized[:available]}"


def short_first_name(value):
    return value[:MAX_FIRST_NAME_LENGTH]


def upsert_demo_user(username, password, first_name, **role_fields):
    user, created = User.objects.get_or_create(
        username=username,
        defaults={
            "first_name": first_name,
            "is_active": True,
            "is_staff": False,
            "is_superuser": False,
        },
    )
    user.first_name = first_name
    user.is_active = True
    user.is_staff = False
    user.is_superuser = False
    user.set_password(password)
    user.save()

    profile, profile_created = UserApp.objects.get_or_create(user=user)
    profile.school = role_fields.get("school")
    profile.subsystem = role_fields.get("subsystem")
    profile.municipality = role_fields.get("municipality")
    profile.level = role_fields.get("level")
    profile.save()

    return created, profile_created


class Command(BaseCommand):
    help = "Create demo users for schools, subsystems, municipalities and global levels."

    def add_arguments(self, parser):
        parser.add_argument(
            "--password",
            default=DEFAULT_PASSWORD,
            help="Password assigned to all generated demo users.",
        )

    def handle(self, *args, **options):
        password = options["password"]

        schools = list(School.objects.select_related("subsystem", "muni").order_by("school_key"))
        subsystems = list(Subsystem.objects.order_by("abrev"))
        municipalities = list(Municipality.objects.select_related("state").order_by("key"))

        if not schools:
            raise CommandError("No schools found. Run seed_demo_schools first.")
        if not subsystems:
            raise CommandError("No subsystems found. Run seed_jalisco_catalogs first.")
        if not municipalities:
            raise CommandError("No municipalities found. Run seed_jalisco_catalogs first.")

        try:
            jalisco = State.objects.get(name="Jalisco")
        except State.DoesNotExist as exc:
            raise CommandError("State 'Jalisco' not found.") from exc

        user_created = 0
        profile_created = 0

        for school in schools:
            created, profile_new = upsert_demo_user(
                username=username_with_prefix("plantel", school.school_key),
                password=password,
                first_name=short_first_name(f"Demo Plantel {school.school_key}"),
                school=school,
            )
            user_created += int(created)
            profile_created += int(profile_new)

        for subsystem in subsystems:
            created, profile_new = upsert_demo_user(
                username=username_with_prefix("subsystem", subsystem.abrev),
                password=password,
                first_name=short_first_name(f"Demo Subsistema {subsystem.abrev}"),
                subsystem=subsystem,
            )
            user_created += int(created)
            profile_created += int(profile_new)

        for municipality in municipalities:
            created, profile_new = upsert_demo_user(
                username=username_with_prefix("municipio", municipality.key),
                password=password,
                first_name=short_first_name(f"Demo Municipio {municipality.name}"),
                municipality=municipality,
                level=3,
            )
            user_created += int(created)
            profile_created += int(profile_new)

        for level, label in [(0, "secundaria"), (1, "media_superior"), (2, "superior")]:
            created, profile_new = upsert_demo_user(
                username=f"sistema_{label}",
                password=password,
                first_name=short_first_name(f"Demo Sistema {label}"),
                level=level,
            )
            user_created += int(created)
            profile_created += int(profile_new)

        created, profile_new = upsert_demo_user(
            username="estado_jalisco",
            password=password,
            first_name=short_first_name("Demo Estado Jalisco"),
            municipality=municipalities[0],
            level=4,
        )
        user_created += int(created)
        profile_created += int(profile_new)

        self.stdout.write(
            self.style.SUCCESS(
                (
                    f"Usuarios demo creados={user_created}. "
                    f"Perfiles nuevos={profile_created}. "
                    f"Password comun={password}"
                )
            )
        )
