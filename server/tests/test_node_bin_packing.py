"""Tests for filter_by_node_capacity (per-node bin-packing precheck).

See docs/architecture/dispatcher.md §2.6.
"""

from sqlalchemy import text

from cjob.config import Settings
from cjob.dispatcher.scheduler import filter_by_node_capacity
from cjob.models import Job


NS = "alice"
NS2 = "bob"


def _make_job(
    namespace,
    job_id,
    user="alice",
    flavor="cpu",
    cpu_millicores=1000,
    memory_mib=1024,
    gpu=0,
    completions=None,
    parallelism=None,
    status="QUEUED",
):
    """Build a transient Job for filter_by_node_capacity tests."""
    return Job(
        namespace=namespace,
        job_id=job_id,
        user=user,
        image="test:1.0",
        command="python main.py",
        cwd="/home/jovyan",
        env_json={},
        cpu="1",
        memory="1Gi",
        gpu=gpu,
        flavor=flavor,
        time_limit_seconds=86400,
        status=status,
        log_dir=f"/home/jovyan/.cjob/logs/{job_id}",
        cpu_millicores=cpu_millicores,
        memory_mib=memory_mib,
        completions=completions,
        parallelism=parallelism,
    )


def _insert_node(session, node_name, flavor="cpu", cpu=100000, mem=500000, gpu=0):
    session.execute(
        text(
            "INSERT INTO node_resources "
            "(node_name, cpu_millicores, memory_mib, gpu, flavor) "
            "VALUES (:name, :cpu, :mem, :gpu, :flavor)"
        ),
        {"name": node_name, "cpu": cpu, "mem": mem, "gpu": gpu, "flavor": flavor},
    )
    session.flush()


def _insert_running_job(
    session,
    namespace,
    job_id,
    flavor="cpu",
    cpu_millicores=1000,
    memory_mib=1024,
    gpu=0,
    node_name=None,
    completions=None,
    parallelism=None,
    user="alice",
):
    """Insert a RUNNING job directly via SQL (avoids ORM type juggling)."""
    session.execute(
        text(
            "INSERT INTO jobs "
            "(namespace, job_id, \"user\", image, command, cwd, env_json, "
            " cpu, memory, gpu, flavor, time_limit_seconds, status, "
            " cpu_millicores, memory_mib, node_name, completions, parallelism, log_dir) "
            "VALUES (:ns, :jid, :u, 't:1', 'cmd', '/home/jovyan', '{}', "
            " '1', '1Gi', :gpu, :flavor, 86400, 'RUNNING', "
            " :cpum, :memm, :node, :compl, :par, '/log')"
        ),
        {
            "ns": namespace,
            "jid": job_id,
            "u": user,
            "gpu": gpu,
            "flavor": flavor,
            "cpum": cpu_millicores,
            "memm": memory_mib,
            "node": node_name,
            "compl": completions,
            "par": parallelism,
        },
    )
    session.flush()


def _insert_in_flight_job(
    session,
    namespace,
    job_id,
    status="DISPATCHED",
    flavor="cpu",
    cpu_millicores=1000,
    memory_mib=1024,
    gpu=0,
    completions=None,
    parallelism=None,
    user="alice",
):
    session.execute(
        text(
            "INSERT INTO jobs "
            "(namespace, job_id, \"user\", image, command, cwd, env_json, "
            " cpu, memory, gpu, flavor, time_limit_seconds, status, "
            " cpu_millicores, memory_mib, completions, parallelism, log_dir) "
            "VALUES (:ns, :jid, :u, 't:1', 'cmd', '/home/jovyan', '{}', "
            " '1', '1Gi', :gpu, :flavor, 86400, :st, "
            " :cpum, :memm, :compl, :par, '/log')"
        ),
        {
            "ns": namespace,
            "jid": job_id,
            "u": user,
            "gpu": gpu,
            "flavor": flavor,
            "cpum": cpu_millicores,
            "memm": memory_mib,
            "st": status,
            "compl": completions,
            "par": parallelism,
        },
    )
    session.flush()


def _settings(enabled=True):
    return Settings(POSTGRES_PASSWORD="test", NODE_BIN_PACKING_ENABLED=enabled)


# ── Enable/disable & fallback ──


class TestEnableAndFallback:
    def test_disabled_passes_all(self, db_session):
        """NODE_BIN_PACKING_ENABLED=False → candidates unchanged."""
        # No nodes inserted; would normally fall back to unrestricted anyway,
        # but disable is checked first.
        candidates = [_make_job(NS, 1)]
        result = filter_by_node_capacity(db_session, candidates, _settings(enabled=False))
        assert result == candidates

    def test_empty_candidates(self, db_session):
        result = filter_by_node_capacity(db_session, [], _settings())
        assert result == []

    def test_node_resources_empty_passes_all(self, db_session):
        """node_resources empty (Watcher not running) → unrestricted."""
        candidates = [_make_job(NS, 1, cpu_millicores=999_999, memory_mib=999_999)]
        result = filter_by_node_capacity(db_session, candidates, _settings())
        assert len(result) == 1

    def test_flavor_not_in_node_resources_passes(self, db_session):
        """Candidate's flavor not present in node_resources → unrestricted."""
        _insert_node(db_session, "node-1", flavor="cpu", cpu=10000, mem=10000)
        candidates = [_make_job(NS, 1, flavor="gpu-a100", cpu_millicores=999_999)]
        result = filter_by_node_capacity(db_session, candidates, _settings())
        assert len(result) == 1


# ── Single-pod (non-sweep) admission ──


class TestNonSweepAdmission:
    def test_fits_on_only_node(self, db_session):
        _insert_node(db_session, "node-1", cpu=10000, mem=20000)
        j1 = _make_job(NS, 1, cpu_millicores=2000, memory_mib=4096)
        result = filter_by_node_capacity(db_session, [j1], _settings())
        assert result == [j1]

    def test_does_not_fit_anywhere(self, db_session):
        _insert_node(db_session, "node-1", cpu=1000, mem=1024)
        _insert_node(db_session, "node-2", cpu=1000, mem=1024)
        j1 = _make_job(NS, 1, cpu_millicores=5000, memory_mib=4096)
        result = filter_by_node_capacity(db_session, [j1], _settings())
        assert result == []

    def test_fits_on_one_of_many(self, db_session):
        _insert_node(db_session, "node-small", cpu=1000, mem=1024)
        _insert_node(db_session, "node-large", cpu=10000, mem=20000)
        j1 = _make_job(NS, 1, cpu_millicores=5000, memory_mib=4096)
        result = filter_by_node_capacity(db_session, [j1], _settings())
        assert result == [j1]

    def test_gpu_check(self, db_session):
        """GPU requirement rejected when no node has GPU."""
        _insert_node(db_session, "node-cpu", flavor="cpu", cpu=10000, mem=10000, gpu=0)
        j1 = _make_job(NS, 1, flavor="cpu", cpu_millicores=1000, memory_mib=1024, gpu=1)
        result = filter_by_node_capacity(db_session, [j1], _settings())
        assert result == []

    def test_memory_check(self, db_session):
        """Memory exceeds → reject even if CPU fits."""
        _insert_node(db_session, "node-1", cpu=10000, mem=1024)
        j1 = _make_job(NS, 1, cpu_millicores=1000, memory_mib=8192)
        result = filter_by_node_capacity(db_session, [j1], _settings())
        assert result == []


# ── Same-cycle cumulative tracking ──


class TestCumulativeTracking:
    def test_subsequent_candidate_uses_updated_residual(self, db_session):
        """Best-fit places candidates on different nodes within a cycle."""
        # Two equal nodes; each can hold one job of size 3000.
        _insert_node(db_session, "node-1", cpu=4000, mem=10000)
        _insert_node(db_session, "node-2", cpu=4000, mem=10000)
        j1 = _make_job(NS, 1, cpu_millicores=3000, memory_mib=1024)
        j2 = _make_job(NS, 2, cpu_millicores=3000, memory_mib=1024)
        j3 = _make_job(NS, 3, cpu_millicores=3000, memory_mib=1024)
        result = filter_by_node_capacity(db_session, [j1, j2, j3], _settings())
        # j1, j2 fill node-1 and node-2 (3000 each); j3 has no fit (1000 left each)
        assert [j.job_id for j in result] == [1, 2]

    def test_least_loaded_spreads_candidates(self, db_session):
        """Least-loaded placement spreads candidates across nodes."""
        # Two equal-sized nodes.
        _insert_node(db_session, "node-a", cpu=10000, mem=10000)
        _insert_node(db_session, "node-b", cpu=10000, mem=10000)
        j1 = _make_job(NS, 1, cpu_millicores=4000, memory_mib=1024)
        # Place on node-a (largest residual; tie-breaker by sort order).
        # After j1: node-a=6000, node-b=10000. j2 should go to node-b
        # (now largest), leaving node-a=6000, node-b=2000.
        j2 = _make_job(NS, 2, cpu_millicores=8000, memory_mib=1024)
        # j3 needs 6000m → fits node-a, but not node-b (2000m).
        j3 = _make_job(NS, 3, cpu_millicores=6000, memory_mib=1024)
        result = filter_by_node_capacity(db_session, [j1, j2, j3], _settings())
        assert [j.job_id for j in result] == [1, 2, 3]


# ── RUNNING consumption subtraction ──


class TestRunningSubtraction:
    def test_running_non_sweep_subtracted(self, db_session):
        """RUNNING non-sweep job reduces residual of its node."""
        _insert_node(db_session, "node-1", cpu=10000, mem=10000)
        _insert_running_job(
            db_session, NS, 1, cpu_millicores=8000, memory_mib=4096,
            node_name="node-1",
        )
        # Only 2000m CPU left after subtraction.
        j2 = _make_job(NS, 2, cpu_millicores=3000, memory_mib=1024)
        result = filter_by_node_capacity(db_session, [j2], _settings())
        assert result == []

    def test_running_non_sweep_subtracted_only_from_listed_node(self, db_session):
        _insert_node(db_session, "node-1", cpu=10000, mem=10000)
        _insert_node(db_session, "node-2", cpu=10000, mem=10000)
        _insert_running_job(
            db_session, NS, 1, cpu_millicores=8000, memory_mib=4096,
            node_name="node-1",
        )
        # node-2 should still have full 10000 CPU; new job fits there.
        j2 = _make_job(NS, 2, cpu_millicores=8000, memory_mib=4096)
        result = filter_by_node_capacity(db_session, [j2], _settings())
        assert result == [j2]

    def test_running_no_node_name_uses_least_loaded(self, db_session):
        """RUNNING job without node_name (completion fallback) is virtually placed."""
        _insert_node(db_session, "node-1", cpu=10000, mem=10000)
        _insert_running_job(
            db_session, NS, 1, cpu_millicores=8000, memory_mib=4096,
            node_name=None,
        )
        j2 = _make_job(NS, 2, cpu_millicores=3000, memory_mib=1024)
        # Virtually placed onto node-1 (only fitting node) → residual 2000m
        result = filter_by_node_capacity(db_session, [j2], _settings())
        assert result == []

    def test_running_sweep_even_distribution(self, db_session):
        """Sweep RUNNING distributes parallelism evenly across listed nodes."""
        _insert_node(db_session, "node-1", cpu=10000, mem=10000)
        _insert_node(db_session, "node-2", cpu=10000, mem=10000)
        # parallelism=4 over 2 listed nodes → 2 pods each, 2000m each
        _insert_running_job(
            db_session, NS, 1, cpu_millicores=1000, memory_mib=1024,
            node_name="node-1,node-2", completions=10, parallelism=4,
        )
        # Each node now has 8000m residual. A 3000m candidate fits on either.
        j2 = _make_job(NS, 2, cpu_millicores=3000, memory_mib=1024)
        result = filter_by_node_capacity(db_session, [j2], _settings())
        assert result == [j2]

    def test_running_sweep_uneven_remainder(self, db_session):
        """Sweep parallelism that doesn't divide evenly: remainder goes to front."""
        _insert_node(db_session, "node-1", cpu=10000, mem=10000)
        _insert_node(db_session, "node-2", cpu=10000, mem=10000)
        # parallelism=5 over 2 nodes → node-1: 3 pods, node-2: 2 pods
        _insert_running_job(
            db_session, NS, 1, cpu_millicores=1000, memory_mib=1024,
            node_name="node-1,node-2", completions=10, parallelism=5,
        )
        # node-1: 10000 - 3000 = 7000; node-2: 10000 - 2000 = 8000
        # Candidate needing 7500 fits on node-2 only.
        j2 = _make_job(NS, 2, cpu_millicores=7500, memory_mib=1024)
        result = filter_by_node_capacity(db_session, [j2], _settings())
        assert result == [j2]
        # Candidate needing 8500 fits on neither.
        j3 = _make_job(NS, 3, cpu_millicores=8500, memory_mib=1024)
        result = filter_by_node_capacity(db_session, [j3], _settings())
        assert result == []


# ── DISPATCHING/DISPATCHED in-flight subtraction ──


class TestInFlightSubtraction:
    def test_dispatched_in_flight_subtracted(self, db_session):
        """DISPATCHED job is best-fit placed, reducing residual."""
        _insert_node(db_session, "node-1", cpu=10000, mem=10000)
        _insert_in_flight_job(
            db_session, NS, 1, status="DISPATCHED",
            cpu_millicores=8000, memory_mib=4096,
        )
        j2 = _make_job(NS, 2, cpu_millicores=3000, memory_mib=1024)
        result = filter_by_node_capacity(db_session, [j2], _settings())
        # node-1 residual after virtual placement: 2000m → 3000m doesn't fit.
        assert result == []

    def test_dispatching_in_flight_subtracted(self, db_session):
        _insert_node(db_session, "node-1", cpu=10000, mem=10000)
        _insert_in_flight_job(
            db_session, NS, 1, status="DISPATCHING",
            cpu_millicores=8000, memory_mib=4096,
        )
        j2 = _make_job(NS, 2, cpu_millicores=3000, memory_mib=1024)
        result = filter_by_node_capacity(db_session, [j2], _settings())
        assert result == []

    def test_in_flight_sweep_uses_parallelism(self, db_session):
        """In-flight sweep places `parallelism` pods via best-fit."""
        _insert_node(db_session, "node-1", cpu=10000, mem=10000)
        _insert_node(db_session, "node-2", cpu=10000, mem=10000)
        # Sweep with parallelism=4, 3000m per pod → 4 pods total = 12000m
        # Best-fit distributes them.
        _insert_in_flight_job(
            db_session, NS, 1, status="DISPATCHED",
            cpu_millicores=3000, memory_mib=1024,
            completions=10, parallelism=4,
        )
        # Two nodes each receive 2 pods (4×3000m=12000m total) → 4000m each
        # remaining. A 5000m candidate doesn't fit on either node.
        j2 = _make_job(NS, 2, cpu_millicores=5000, memory_mib=1024)
        result = filter_by_node_capacity(db_session, [j2], _settings())
        assert result == []


# ── Sweep candidate admission ──


class TestSweepAdmission:
    def test_sweep_all_pods_fit(self, db_session):
        """Sweep admitted when all parallelism pods can be placed."""
        _insert_node(db_session, "node-1", cpu=10000, mem=10000)
        _insert_node(db_session, "node-2", cpu=10000, mem=10000)
        # parallelism=4, 3000m per pod → 12000m total, distributable across 2 nodes
        sw = _make_job(
            NS, 1, cpu_millicores=3000, memory_mib=1024,
            completions=10, parallelism=4,
        )
        result = filter_by_node_capacity(db_session, [sw], _settings())
        assert result == [sw]

    def test_sweep_partial_fit_rejected(self, db_session):
        """Sweep rejected when not all pods can be placed."""
        _insert_node(db_session, "node-1", cpu=4000, mem=10000)
        _insert_node(db_session, "node-2", cpu=4000, mem=10000)
        # parallelism=4, 3000m per pod → first 2 fit (one on each node), then
        # remaining residual is 1000m per node and the next 3000m pod can't be placed.
        sw = _make_job(
            NS, 1, cpu_millicores=3000, memory_mib=1024,
            completions=10, parallelism=4,
        )
        result = filter_by_node_capacity(db_session, [sw], _settings())
        assert result == []

    def test_sweep_all_on_one_node(self, db_session):
        """Sweep can pack multiple pods onto the same node if it has capacity."""
        _insert_node(db_session, "node-big", cpu=20000, mem=20000)
        sw = _make_job(
            NS, 1, cpu_millicores=3000, memory_mib=1024,
            completions=10, parallelism=4,
        )
        result = filter_by_node_capacity(db_session, [sw], _settings())
        assert result == [sw]


# ── Flavor isolation ──


class TestFlavorIsolation:
    def test_different_flavors_independent(self, db_session):
        """CPU candidates don't see GPU node residuals and vice versa."""
        _insert_node(db_session, "cpu-1", flavor="cpu", cpu=10000, mem=10000, gpu=0)
        _insert_node(db_session, "gpu-1", flavor="gpu-a100", cpu=10000, mem=10000, gpu=4)
        # A CPU candidate that wouldn't fit any GPU node still admits via cpu-1.
        j_cpu = _make_job(NS, 1, flavor="cpu", cpu_millicores=5000, memory_mib=1024)
        # A GPU candidate that needs GPU goes to GPU node.
        j_gpu = _make_job(
            NS, 2, flavor="gpu-a100", cpu_millicores=5000, memory_mib=1024, gpu=1,
        )
        result = filter_by_node_capacity(db_session, [j_cpu, j_gpu], _settings())
        assert {j.job_id for j in result} == {1, 2}

    def test_cross_flavor_running_does_not_subtract(self, db_session):
        """RUNNING job on a different flavor's node doesn't affect this flavor."""
        _insert_node(db_session, "cpu-1", flavor="cpu", cpu=10000, mem=10000)
        _insert_node(db_session, "gpu-1", flavor="gpu-a100", cpu=10000, mem=10000, gpu=4)
        # RUNNING job on GPU side
        _insert_running_job(
            db_session, NS, 99, flavor="gpu-a100",
            cpu_millicores=8000, memory_mib=4096,
            node_name="gpu-1",
        )
        # CPU candidate sees cpu-1 fully available.
        j1 = _make_job(NS, 1, flavor="cpu", cpu_millicores=8000, memory_mib=4096)
        result = filter_by_node_capacity(db_session, [j1], _settings())
        assert result == [j1]


# ── Bug reproduction ──


class TestBugReproduction:
    def test_issue_203_scenario(self, db_session):
        """Reproduce the bug from issue #203.

        Cluster: 103 cores / 500Gi memory × 2 nodes.
        Two 20 cores / 300Gi jobs already RUNNING (one on each node).
        Third 20 cores / 300Gi job submitted → must be rejected because no
        node has 300Gi free (each has 200Gi left).
        """
        _insert_node(
            db_session, "node-1", flavor="cpu",
            cpu=103_000, mem=500 * 1024,
        )
        _insert_node(
            db_session, "node-2", flavor="cpu",
            cpu=103_000, mem=500 * 1024,
        )
        _insert_running_job(
            db_session, NS, 1,
            cpu_millicores=20_000, memory_mib=300 * 1024,
            node_name="node-1",
        )
        _insert_running_job(
            db_session, NS, 2,
            cpu_millicores=20_000, memory_mib=300 * 1024,
            node_name="node-2",
        )
        third = _make_job(
            NS, 3, cpu_millicores=20_000, memory_mib=300 * 1024,
        )
        result = filter_by_node_capacity(db_session, [third], _settings())
        assert result == [], (
            "Expected third 300Gi job to be held in QUEUED because no node "
            "has 300Gi free (both nodes have 200Gi residual)."
        )

    def test_issue_203_scenario_third_fits_after_first_completes(self, db_session):
        """When one of the first two jobs is no longer RUNNING, the third fits."""
        _insert_node(db_session, "node-1", flavor="cpu", cpu=103_000, mem=500 * 1024)
        _insert_node(db_session, "node-2", flavor="cpu", cpu=103_000, mem=500 * 1024)
        # Only job on node-1 is RUNNING; node-2 is fully free.
        _insert_running_job(
            db_session, NS, 1,
            cpu_millicores=20_000, memory_mib=300 * 1024,
            node_name="node-1",
        )
        third = _make_job(NS, 3, cpu_millicores=20_000, memory_mib=300 * 1024)
        result = filter_by_node_capacity(db_session, [third], _settings())
        assert result == [third]


# ── Mixed namespaces ──


class TestMixedNamespaces:
    def test_multiple_namespaces_share_residuals(self, db_session):
        """Candidates from different namespaces compete for the same node residuals."""
        _insert_node(db_session, "node-1", cpu=6000, mem=10000)
        j1 = _make_job(NS, 1, cpu_millicores=3000, memory_mib=1024)
        j2 = _make_job(NS2, 1, user="bob", cpu_millicores=3000, memory_mib=1024)
        j3 = _make_job(NS, 2, cpu_millicores=3000, memory_mib=1024)
        result = filter_by_node_capacity(db_session, [j1, j2, j3], _settings())
        # node-1 holds two 3000m candidates; the third has no fit.
        assert len(result) == 2
