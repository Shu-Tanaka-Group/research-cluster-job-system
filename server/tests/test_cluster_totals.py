from sqlalchemy import text

from cjob.dispatcher.scheduler import _fetch_flavor_caps


class TestFetchFlavorCaps:
    def test_empty_table(self, db_session):
        """Empty node_resources returns empty dict."""
        caps = _fetch_flavor_caps(db_session)
        assert caps == {}

    def test_single_node(self, db_session):
        db_session.execute(
            text(
                "INSERT INTO node_resources (node_name, cpu_millicores, memory_mib, gpu) "
                "VALUES ('node-1', 64000, 262144, 4)"
            )
        )
        db_session.flush()

        caps = _fetch_flavor_caps(db_session)
        # Default flavor is 'cpu' (column default)
        assert "cpu" in caps
        assert caps["cpu"]["cpu"] == 64000.0
        assert caps["cpu"]["mem"] == 262144.0
        assert caps["cpu"]["gpu"] == 4.0
        assert caps["cpu"]["weight"] == 1.0

    def test_multiple_nodes_sums(self, db_session):
        db_session.execute(
            text(
                "INSERT INTO node_resources (node_name, cpu_millicores, memory_mib, gpu) "
                "VALUES ('node-1', 32000, 131072, 0), "
                "       ('node-2', 64000, 262144, 4), "
                "       ('node-3', 32000, 131072, 2)"
            )
        )
        db_session.flush()

        caps = _fetch_flavor_caps(db_session)
        assert caps["cpu"]["cpu"] == 128000.0  # 32000 + 64000 + 32000
        assert caps["cpu"]["mem"] == 524288.0  # 131072 + 262144 + 131072
        assert caps["cpu"]["gpu"] == 6.0       # 0 + 4 + 2

    def test_capacity_capped_by_quota(self, db_session):
        """Capacity should be MIN(allocatable, nominalQuota)."""
        db_session.execute(
            text(
                "INSERT INTO node_resources (node_name, cpu_millicores, memory_mib, gpu, flavor) "
                "VALUES ('node-1', 128000, 524288, 0, 'cpu')"
            )
        )
        db_session.execute(
            text(
                "INSERT INTO flavor_quotas (flavor, cpu, memory, gpu, drf_weight) "
                "VALUES ('cpu', '256', '1000Gi', '0', 2.0)"
            )
        )
        db_session.flush()

        caps = _fetch_flavor_caps(db_session)
        # MIN(128000, 256000) = 128000 (weight NOT multiplied into capacity)
        assert caps["cpu"]["cpu"] == 128000.0
        # MIN(524288, 1024000) = 524288
        assert caps["cpu"]["mem"] == 524288.0
        assert caps["cpu"]["weight"] == 2.0

    def test_multiple_flavors(self, db_session):
        """Per-flavor caps should be returned separately."""
        db_session.execute(
            text(
                "INSERT INTO node_resources (node_name, cpu_millicores, memory_mib, gpu, flavor) "
                "VALUES ('node-1', 128000, 524288, 0, 'cpu'), "
                "       ('node-2', 32000, 131072, 4, 'gpu')"
            )
        )
        db_session.execute(
            text(
                "INSERT INTO flavor_quotas (flavor, cpu, memory, gpu, drf_weight) "
                "VALUES ('cpu', '256', '1000Gi', '0', 1.0), "
                "       ('gpu', '64', '500Gi', '8', 2.0)"
            )
        )
        db_session.flush()

        caps = _fetch_flavor_caps(db_session)
        assert len(caps) == 2

        # cpu: MIN(128000, 256000) = 128000
        assert caps["cpu"]["cpu"] == 128000.0
        assert caps["cpu"]["weight"] == 1.0

        # gpu: MIN(32000, 64000) = 32000, MIN(4, 8) = 4
        assert caps["gpu"]["cpu"] == 32000.0
        assert caps["gpu"]["gpu"] == 4.0
        assert caps["gpu"]["weight"] == 2.0

    def test_default_weight_when_no_quota(self, db_session):
        """Flavor without quota row should use weight 1.0 (raw allocatable)."""
        db_session.execute(
            text(
                "INSERT INTO node_resources (node_name, cpu_millicores, memory_mib, gpu, flavor) "
                "VALUES ('node-1', 64000, 262144, 0, 'cpu')"
            )
        )
        # No flavor_quotas row
        db_session.flush()

        caps = _fetch_flavor_caps(db_session)
        assert caps["cpu"]["cpu"] == 64000.0  # raw allocatable
        assert caps["cpu"]["mem"] == 262144.0
        assert caps["cpu"]["weight"] == 1.0
