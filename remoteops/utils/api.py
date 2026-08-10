import ctypes
from typing import List, Tuple

# kernel32 só existe no Windows — lazy para testes/lint
_kernel32 = None


def _get_kernel32():
    global _kernel32
    if _kernel32 is None:
        from ctypes import wintypes

        k = ctypes.windll.kernel32
        k.GetActiveProcessorGroupCount.restype = wintypes.WORD
        k.GetActiveProcessorCount.restype = wintypes.DWORD
        k.GetActiveProcessorCount.argtypes = [wintypes.WORD]
        _kernel32 = k
    return _kernel32


def get_processor_groups() -> List[int]:
    try:
        kernel32 = _get_kernel32()
        group_count = kernel32.GetActiveProcessorGroupCount()
        return list(range(group_count))
    except Exception as e:
        print(f"Erro ao obter grupos de processador: {e}")
        return [0]


def get_processor_count(group_id: int) -> int:
    try:
        kernel32 = _get_kernel32()
        cpu_count = kernel32.GetActiveProcessorCount(group_id)
        return cpu_count
    except Exception as e:
        print(f"Erro ao obter CPUs do grupo {group_id}: {e}")
        return 1


def get_all_processor_info() -> List[Tuple[int, int]]:
    groups = get_processor_groups()
    return [(gid, get_processor_count(gid)) for gid in groups]


def validate_affinity_mask(affinity_text: str, max_cpu: int) -> bool:
    if not affinity_text.strip():
        return True
    try:
        cpu_list = [int(x.strip()) for x in affinity_text.split(",") if x.strip()]
        for cpu in cpu_list:
            if cpu < 1 or cpu > max_cpu:
                return False
        return len(cpu_list) == len(set(cpu_list))
    except ValueError:
        return False


def format_affinity_mask(cpu_list: List[int]) -> str:
    if not cpu_list:
        return ""
    return ",".join(map(str, sorted(cpu_list)))


def parse_affinity_mask(affinity_text: str) -> List[int]:
    if not affinity_text.strip():
        return []
    try:
        return sorted([int(x.strip()) for x in affinity_text.split(",") if x.strip()])
    except ValueError:
        return []
