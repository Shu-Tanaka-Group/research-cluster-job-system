import math


def parse_cpu_millicores(cpu: str) -> int:
    """Convert CPU string (e.g. "2", "0.5", "500m") to millicores."""
    if cpu.endswith("m"):
        return int(cpu[:-1])
    return int(math.ceil(float(cpu) * 1000))


def parse_memory_mib(memory: str) -> int:
    """Convert Kubernetes memory quantity string to MiB.

    Supports all Kubernetes resource.Quantity suffixes:
    - Binary: Ki, Mi, Gi, Ti, Pi, Ei
    - Decimal: m (milli), k, M, G, T, P, E
    - Plain integer (bytes)
    """
    MIB = 1024 * 1024  # bytes per MiB

    # Binary suffixes (check 2-char suffixes first)
    if memory.endswith("Ei"):
        return int(math.ceil(float(memory[:-2]) * 1024**4))
    if memory.endswith("Pi"):
        return int(math.ceil(float(memory[:-2]) * 1024**3))
    if memory.endswith("Ti"):
        return int(math.ceil(float(memory[:-2]) * 1024**2))
    if memory.endswith("Gi"):
        return int(math.ceil(float(memory[:-2]) * 1024))
    if memory.endswith("Mi"):
        return int(math.ceil(float(memory[:-2])))
    if memory.endswith("Ki"):
        return int(math.ceil(float(memory[:-2]) / 1024))

    # Decimal suffixes (single-char, check after 2-char)
    if memory.endswith("E"):
        return int(math.ceil(float(memory[:-1]) * 10**18 / MIB))
    if memory.endswith("P"):
        return int(math.ceil(float(memory[:-1]) * 10**15 / MIB))
    if memory.endswith("T"):
        return int(math.ceil(float(memory[:-1]) * 10**12 / MIB))
    if memory.endswith("G"):
        return int(math.ceil(float(memory[:-1]) * 10**9 / MIB))
    if memory.endswith("M"):
        return int(math.ceil(float(memory[:-1]) * 10**6 / MIB))
    if memory.endswith("k"):
        return int(math.ceil(float(memory[:-1]) * 10**3 / MIB))
    if memory.endswith("m"):
        return int(math.ceil(int(memory[:-1]) / 1000 / MIB))

    # Plain bytes
    return int(math.ceil(int(memory) / MIB))
