import factory
from factory.django import DjangoModelFactory

from apps.accounts.models import Branch, RegisteredDevice, Role, User

DEFAULT_TEST_PASSWORD = "test-pass-12345"


class BranchFactory(DjangoModelFactory):
    class Meta:
        model = Branch
        django_get_or_create = ("code",)

    code = factory.Sequence(lambda n: f"VS{n:02d}")
    name = factory.Faker("company")
    receipt_prefix = "VS"
    is_active = True


class UserFactory(DjangoModelFactory):
    class Meta:
        model = User
        django_get_or_create = ("username",)
        skip_postgeneration_save = True

    username = factory.Sequence(lambda n: f"user{n}")
    first_name = factory.Faker("first_name")
    role = Role.EMPLOYEE
    branch = factory.SubFactory(BranchFactory)

    @factory.post_generation
    def password(obj, create, extracted, **kwargs):
        obj.set_password(extracted or DEFAULT_TEST_PASSWORD)
        if create:
            obj.save()


class OwnerFactory(UserFactory):
    username = factory.Sequence(lambda n: f"owner{n}")
    role = Role.OWNER


class EmployeeFactory(UserFactory):
    username = factory.Sequence(lambda n: f"employee{n}")
    role = Role.EMPLOYEE


class TechAdminFactory(UserFactory):
    username = factory.Sequence(lambda n: f"techadmin{n}")
    role = Role.TECH_ADMIN
    branch = None
    is_staff = True
    is_superuser = True


class RegisteredDeviceFactory(DjangoModelFactory):
    class Meta:
        model = RegisteredDevice

    branch = factory.SubFactory(BranchFactory)
    name = factory.Sequence(lambda n: f"Till device {n}")
    registered_by = factory.SubFactory(TechAdminFactory)
