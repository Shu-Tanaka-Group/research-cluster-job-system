from sqlalchemy import text

from cjob.dispatcher.scheduler import _fetch_cluster_totals


class TestFetchClusterTotals:
    def test_empty_table(self, db_session):
        """Empty node_resources returns all zeros."""
        cpu, mem, gpu = _fetch_cluster_totals(db_session)
        assert cpu == 0
        assert mem == 0
        assert gpu == 0

    def test_single_node(self, db_session):
        db_session.execute(
            text(
                "INSERT INTO node_resources (node_name, cpu_millicores, memory_mib, gpu) "
                "VALUES ('node-1', 64000, 262144, 4)"
            )
        )
        db_session.flush()

        cpu, mem, gpu = _fetch_cluster_totals(db_session)
        assert cpu == 64000
        assert mem == 262144
        assert gpu == 4

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

        cpu, mem, gpu = _fetch_cluster_totals(db_session)
        assert cpu == 128000  # 32000 + 64000 + 32000
        assert mem == 524288  # 131072 + 262144 + 131072
        assert gpu == 6       # 0 + 4 + 2

    def test_drf_weight_scales_capacity(self, db_session):
        """DRF weight should scale the effective capacity."""
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

        cpu, mem, gpu = _fetch_cluster_totals(db_session)
        # MIN(128000, 256000) * 2.0 = 256000
        assert cpu == 256000.0
        # MIN(524288, 1024000) * 2.0 = 1048576
        assert mem == 1048576.0

    def test_drf_weight_with_multiple_flavors(self, db_session):
        """Weighted totals should sum across multiple flavors."""
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

        cpu, mem, gpu = _fetch_cluster_totals(db_session)
        # cpu: MIN(128000, 256000)*1.0 + MIN(32000, 64000)*2.0 = 128000 + 64000 = 192000
        assert cpu == 192000.0
        # gpu: MIN(0, 0)*1.0 + MIN(4, 8)*2.0 = 0 + 8 = 8
        assert gpu == 8.0

    def test_drf_weight_default_when_no_quota(self, db_session):
        """Flavor without quota row should use weight 1.0 (raw allocatable)."""
        db_session.execute(
            text(
                "INSERT INTO node_resources (node_name, cpu_millicores, memory_mib, gpu, flavor) "
                "VALUES ('node-1', 64000, 262144, 0, 'cpu')"
            )
        )
        # No flavor_quotas row
        db_session.flush()

        cpu, mem, gpu = _fetch_cluster_totals(db_session)
        assert cpu == 64000  # raw allocatable, no weight applied
        assert mem == 262144
