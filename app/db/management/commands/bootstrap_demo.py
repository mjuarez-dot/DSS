from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import BaseCommand

from app.db.models import UserApp


class Command(BaseCommand):
    help = "Bootstrap demo data: admin user plus Jalisco catalogs and subsystems."

    def add_arguments(self, parser):
        parser.add_argument(
            "--admin-username",
            default="admin",
            help="Username for the bootstrap admin user.",
        )
        parser.add_argument(
            "--admin-password",
            default="Admin123!",
            help="Password for the bootstrap admin user.",
        )

    def handle(self, *args, **options):
        username = options["admin_username"]
        password = options["admin_password"]

        user, created = User.objects.get_or_create(
            username=username,
            defaults={
                "is_staff": True,
                "is_superuser": True,
                "is_active": True,
            },
        )
        user.is_staff = True
        user.is_superuser = True
        user.is_active = True
        user.set_password(password)
        user.save()

        _, profile_created = UserApp.objects.get_or_create(user=user)

        call_command("seed_jalisco_catalogs")
        call_command("seed_demo_schools")
        call_command("seed_demo_users")
        call_command("seed_demo_surveys_answers")

        self.stdout.write(
            self.style.SUCCESS(
                (
                    f"Bootstrap completo. "
                    f"admin={username} "
                    f"user_created={created} "
                    f"profile_created={profile_created}"
                )
            )
        )
