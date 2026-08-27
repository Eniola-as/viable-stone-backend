from django.contrib.auth.password_validation import validate_password
from rest_framework import serializers

from apps.accounts.models import Branch, Role, User
from apps.core.serializers import (
    ControlCharSafeModelSerializer,
    ControlCharSafeSerializer,
)


class BranchSerializer(ControlCharSafeModelSerializer):
    class Meta:
        model = Branch
        fields = [
            "id",
            "code",
            "name",
            "phone",
            "address",
            "timezone",
            "receipt_prefix",
            "is_active",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]


class LoginSerializer(ControlCharSafeSerializer):
    username = serializers.CharField(write_only=True)
    password = serializers.CharField(write_only=True, style={"input_type": "password"})


class PasswordChangeSerializer(ControlCharSafeSerializer):
    current_password = serializers.CharField(
        write_only=True, style={"input_type": "password"}
    )
    new_password = serializers.CharField(
        write_only=True, style={"input_type": "password"}
    )

    def validate_new_password(self, value):
        validate_password(value, user=self.context["request"].user)
        return value


class MFASetupResponseSerializer(ControlCharSafeSerializer):
    secret = serializers.CharField(read_only=True)
    otpauth_url = serializers.CharField(read_only=True)


class TokenSerializer(ControlCharSafeSerializer):
    token = serializers.CharField(write_only=True)


class RecoveryCodeSerializer(ControlCharSafeSerializer):
    code = serializers.CharField(write_only=True)


class RecoveryCodesResponseSerializer(ControlCharSafeSerializer):
    recovery_codes = serializers.ListField(
        child=serializers.CharField(), read_only=True
    )


class UserSelfSerializer(ControlCharSafeModelSerializer):
    """The signed-in user's own safe profile. No cost/secret/audit fields."""

    branch_code = serializers.CharField(
        source="branch.code", read_only=True, default=None
    )
    branch_name = serializers.CharField(
        source="branch.name", read_only=True, default=None
    )

    class Meta:
        model = User
        fields = [
            "id",
            "username",
            "first_name",
            "last_name",
            "email",
            "phone",
            "role",
            "branch",
            "branch_code",
            "branch_name",
            "must_change_password",
            "mfa_required",
            "last_login",
        ]
        read_only_fields = fields


class LoginResponseSerializer(ControlCharSafeSerializer):
    mfa_required = serializers.BooleanField(read_only=True)
    mfa_enrolled = serializers.BooleanField(read_only=True)
    mfa_verified = serializers.BooleanField(read_only=True)
    must_change_password = serializers.BooleanField(read_only=True)
    user = UserSelfSerializer(read_only=True, allow_null=True)


class OwnerUserSerializer(ControlCharSafeModelSerializer):
    """Owner-facing user management. Password is write-only; never echoed."""

    password = serializers.CharField(
        write_only=True, required=False, style={"input_type": "password"}
    )

    class Meta:
        model = User
        fields = [
            "id",
            "username",
            "first_name",
            "last_name",
            "email",
            "phone",
            "role",
            "branch",
            "is_active",
            "must_change_password",
            "mfa_required",
            "last_activity_at",
            "date_joined",
            "password",
        ]
        read_only_fields = [
            "id",
            "must_change_password",
            "mfa_required",
            "last_activity_at",
            "date_joined",
        ]

    def validate_role(self, value):
        if value == Role.TECH_ADMIN:
            raise serializers.ValidationError(
                "The owner cannot create technical-administrator accounts."
            )
        return value

    def validate_password(self, value):
        validate_password(value)
        return value

    def validate(self, attrs):
        if self.instance is None and not attrs.get("password"):
            raise serializers.ValidationError(
                {"password": ["A password is required for a new account."]}
            )
        return attrs
