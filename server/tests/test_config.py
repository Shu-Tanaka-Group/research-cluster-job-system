import json

import pytest
from pydantic import ValidationError

from cjob.config import FlavorDefinition, Settings


class TestFlavorDefinition:
    def test_minimal_definition(self):
        f = FlavorDefinition(name="cpu", label_selector="cjob.io/flavor=cpu")
        assert f.name == "cpu"
        assert f.gpu_resource_name is None

    def test_gpu_definition(self):
        f = FlavorDefinition(
            name="gpu",
            label_selector="cjob.io/flavor=gpu",
            gpu_resource_name="nvidia.com/gpu",
        )
        assert f.gpu_resource_name == "nvidia.com/gpu"

    def test_unknown_field_is_rejected(self):
        # A typo in an optional field must fail loudly instead of being
        # silently dropped (issue #209)
        with pytest.raises(ValidationError) as exc:
            FlavorDefinition(
                name="gpu",
                label_selector="cjob.io/flavor=gpu",
                gpu_resouce_name="nvidia.com/gpu",
            )
        assert "gpu_resouce_name" in str(exc.value)

    def test_missing_required_field_is_rejected(self):
        with pytest.raises(ValidationError):
            FlavorDefinition(name="cpu")


class TestSettingsFlavors:
    def test_flavors_parsed_from_json(self):
        s = Settings(
            RESOURCE_FLAVORS=json.dumps(
                [
                    {"name": "cpu", "label_selector": "cjob.io/flavor=cpu"},
                    {
                        "name": "gpu",
                        "label_selector": "cjob.io/flavor=gpu",
                        "gpu_resource_name": "nvidia.com/gpu",
                    },
                ]
            )
        )
        assert [f.name for f in s.flavors] == ["cpu", "gpu"]
        assert s.get_flavor_definition("gpu").gpu_resource_name == "nvidia.com/gpu"
        assert s.get_flavor_definition("missing") is None

    def test_unknown_field_fails_at_flavor_parse(self):
        s = Settings(
            RESOURCE_FLAVORS=json.dumps(
                [
                    {
                        "name": "gpu",
                        "label_selector": "cjob.io/flavor=gpu",
                        "gpu_resouce_name": "nvidia.com/gpu",
                    }
                ]
            )
        )
        with pytest.raises(ValidationError):
            _ = s.flavors

    def test_default_flavors_are_valid(self):
        s = Settings()
        assert [f.name for f in s.flavors] == ["cpu"]
