import math


def parse_cpu_millicores(cpu: str) -> int:
    """Convert CPU string (e.g. "2", "0.5", "500m") to millicores."""
    if cpu.endswith("m"):
        return int(cpu[:-1])
    return int(math.ceil(float(cpu) * 1000))


def parse_memory_mib(memory: str) -> int:
    """Convert memory string (e.g. "4Gi", "500Mi", "1024") to MiB."""
    if memory.endswith("Gi"):
        return int(math.ceil(float(memory[:-2]) * 1024))
    if memory.endswith("Mi"):
        return int(math.ceil(float(memory[:-2])))
    if memory.endswith("Ki"):
        return int(math.ceil(float(memory[:-2]) / 1024))
    # Plain bytes
    return int(math.ceil(int(memory) / (1024 * 1024)))
