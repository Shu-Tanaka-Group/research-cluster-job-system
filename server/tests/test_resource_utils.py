from cjob.resource_utils import parse_cpu_millicores, parse_memory_mib


class TestParseCpuMillicores:
    def test_integer_cores(self):
        assert parse_cpu_millicores("2") == 2000

    def test_fractional_cores(self):
        assert parse_cpu_millicores("0.5") == 500

    def test_millicores_suffix(self):
        assert parse_cpu_millicores("500m") == 500

    def test_one_core(self):
        assert parse_cpu_millicores("1") == 1000

    def test_large_cores(self):
        assert parse_cpu_millicores("64") == 64000

    def test_small_millicores(self):
        assert parse_cpu_millicores("100m") == 100


class TestParseMemoryMib:
    def test_gi_suffix(self):
        assert parse_memory_mib("4Gi") == 4096

    def test_mi_suffix(self):
        assert parse_memory_mib("500Mi") == 500

    def test_ki_suffix(self):
        assert parse_memory_mib("1024Ki") == 1

    def test_plain_bytes(self):
        assert parse_memory_mib("1048576") == 1  # 1 MiB

    def test_large_gi(self):
        assert parse_memory_mib("256Gi") == 262144

    def test_fractional_gi(self):
        assert parse_memory_mib("1.5Gi") == 1536
