from rest_framework import serializers


class ErrorEnvelopeSerializer(serializers.Serializer):
    """The shape of every error response."""

    code = serializers.CharField(help_text="Stable machine-readable error code.")
    message = serializers.CharField(help_text="Human-readable summary.")
    field_errors = serializers.DictField(
        child=serializers.ListField(child=serializers.CharField()),
        help_text="Per-field validation messages, keyed by field name.",
    )
    request_id = serializers.CharField(help_text="Correlates with server logs.")


class HealthSerializer(serializers.Serializer):
    status = serializers.CharField()
    database = serializers.CharField()
    cache = serializers.CharField()
    time = serializers.DateTimeField()
