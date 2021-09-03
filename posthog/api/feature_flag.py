from typing import Any, Dict, Optional, cast

import posthoganalytics
from django.db.models import QuerySet
from rest_framework import authentication, exceptions, request, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from posthog.api.routing import StructuredViewSetMixin
from posthog.api.shared import UserBasicSerializer
from posthog.api.user import UserSerializer
from posthog.auth import PersonalAPIKeyAuthentication, TemporaryTokenAuthentication
from posthog.mixins import AnalyticsDestroyModelMixin
from posthog.models import FeatureFlag
from posthog.models.feature_flag import get_active_feature_flags_v2
from posthog.permissions import ProjectMembershipNecessaryPermissions


class FeatureFlagSerializer(serializers.HyperlinkedModelSerializer):
    created_by = UserBasicSerializer(read_only=True)
    # :TRICKY: Needed for backwards compatibility
    filters = serializers.DictField(source="get_filters", required=False)
    is_simple_flag = serializers.SerializerMethodField()
    rollout_percentage = serializers.SerializerMethodField()

    class Meta:
        model = FeatureFlag
        fields = [
            "id",
            "name",
            "key",
            "filters",
            "deleted",
            "active",
            "created_by",
            "created_at",
            "is_simple_flag",
            "rollout_percentage",
        ]

    # Simple flags are ones that only have rollout_percentage
    #  That means server side libraries are able to gate these flags without calling to the server
    def get_is_simple_flag(self, feature_flag: FeatureFlag) -> bool:
        return len(feature_flag.groups) == 1 and all(
            len(group.get("properties", [])) == 0 for group in feature_flag.groups
        )

    def get_rollout_percentage(self, feature_flag: FeatureFlag) -> Optional[int]:
        if self.get_is_simple_flag(feature_flag):
            return feature_flag.groups[0].get("rollout_percentage")
        else:
            return None

    def validate_key(self, value):
        exclude_kwargs = {}
        if self.instance:
            exclude_kwargs = {"pk": cast(FeatureFlag, self.instance).pk}

        if (
            FeatureFlag.objects.filter(key=value, team=self.context["request"].user.team, deleted=False)
            .exclude(**exclude_kwargs)
            .exists()
        ):
            raise serializers.ValidationError("There is already a feature flag with this key.", code="unique")

        return value

    def create(self, validated_data: Dict, *args: Any, **kwargs: Any) -> FeatureFlag:
        request = self.context["request"]
        validated_data["created_by"] = request.user
        validated_data["team_id"] = self.context["team_id"]
        self._update_filters(validated_data)

        variants = validated_data.get("filters", {}).get("multivariate", {}).get("variants", [])
        variant_rollout_sum = 0
        for variant in variants:
            variant_rollout_sum += variant.get("rollout_percentage")

        if len(variants) > 0 and variant_rollout_sum != 100:
            raise exceptions.ValidationError(
                "Invalid variant definitions: Variant rollout percentages must sum to 100."
            )

        FeatureFlag.objects.filter(key=validated_data["key"], team=request.user.team, deleted=True).delete()
        instance = super().create(validated_data)

        posthoganalytics.capture(
            request.user.distinct_id, "feature flag created", instance.get_analytics_metadata(),
        )

        return instance

    def update(self, instance: FeatureFlag, validated_data: Dict, *args: Any, **kwargs: Any) -> FeatureFlag:
        request = self.context["request"]
        validated_key = validated_data.get("key", None)
        if validated_key:
            FeatureFlag.objects.filter(key=validated_key, team=instance.team, deleted=True).delete()
        self._update_filters(validated_data)
        instance = super().update(instance, validated_data)

        posthoganalytics.capture(
            request.user.distinct_id, "feature flag updated", instance.get_analytics_metadata(),
        )
        return instance

    def _update_filters(self, validated_data):
        if "get_filters" in validated_data:
            validated_data["filters"] = validated_data.pop("get_filters")


class FeatureFlagViewSet(StructuredViewSetMixin, AnalyticsDestroyModelMixin, viewsets.ModelViewSet):
    legacy_team_compatibility = True  # to be moved to a separate Legacy*ViewSet Class

    queryset = FeatureFlag.objects.all()
    serializer_class = FeatureFlagSerializer
    permission_classes = [IsAuthenticated, ProjectMembershipNecessaryPermissions]
    authentication_classes = [
        PersonalAPIKeyAuthentication,
        TemporaryTokenAuthentication,
        authentication.SessionAuthentication,
        authentication.BasicAuthentication,
    ]

    def get_queryset(self) -> QuerySet:
        queryset = super().get_queryset()
        if self.action == "list":
            queryset = queryset.filter(deleted=False)
        return queryset.order_by("-created_at")

    @action(methods=["GET"], detail=False)
    def my_flags(self, request: request.Request, **kwargs):
        if not request.user.is_authenticated:  # for mypy, since 'AnonymousUser' has no 'feature_flag_override'
            raise serializers.ValidationError("Must be authenticated to get feature flags.")
        flags = get_active_feature_flags_v2(self.team, request.user.distinct_id)
        return Response({"distinct_id": request.user.distinct_id, "flags": flags})

    @action(methods=["GET", "POST"], detail=False)
    def override(self, request: request.Request, **kwargs):
        user = request.user
        if not user.is_authenticated:  # for mypy, since 'AnonymousUser' has no 'feature_flag_override'
            raise serializers.ValidationError("Must be authenticated to override feature flags.")
        if request.data.get("feature_flag_override"):
            serializer = UserSerializer(user, context={"request": request})
            user.feature_flag_override = serializer.validate_feature_flag_override(
                request.data.get("feature_flag_override")
            )
            user.save()
        return Response({"feature_flag_override": user.feature_flag_override})
