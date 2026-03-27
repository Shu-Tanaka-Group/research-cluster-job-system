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
