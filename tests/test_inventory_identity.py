"""Identidade de consultas e consistência do cache de inventário."""

from __future__ import annotations

import threading
import unittest

from remoteops.utils.inventory.cache import InventoryCache, normalize_inventory_host
from remoteops.utils.inventory.models import (
    HardwareData,
    InventorySection,
    OverviewData,
    QueryContext,
    QueryStatus,
    SectionResult,
)
from remoteops.utils.inventory.service import InventoryService


class _CollectorGate:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.payloads: list[object] = []

    def add(self, payload: object) -> None:
        self.payloads.append(payload)

    def collect(self, host: str, **_kwargs: object) -> object:
        if not self.payloads:
            raise AssertionError(f"coletor sem payload para {host}")
        payload = self.payloads.pop(0)
        self.started.set()
        if not self.release.wait(timeout=5):
            raise TimeoutError("coletor não foi liberado")
        return payload


def _overview(host: str) -> OverviewData:
    return OverviewData(
        hostname=host,
        manufacturer=f"MFR-{host}",
        model=f"MDL-{host}",
        os_summary=f"OS-{host}",
        status=QueryStatus.OK,
    )


def _hardware(host: str) -> HardwareData:
    return HardwareData(status=QueryStatus.OK, error=f"hw-{host}")


class NormalizeHostTests(unittest.TestCase):
    def test_strips_unc_and_case(self) -> None:
        self.assertEqual(normalize_inventory_host(r"\\HOSTA"), "hosta")
        self.assertEqual(normalize_inventory_host("  HostA  "), "hosta")


class InventoryCacheIdentityTests(unittest.TestCase):
    def test_old_put_after_host_change_is_rejected(self) -> None:
        cache = InventoryCache()
        query_a = cache.begin_query("HOSTA", InventorySection.OVERVIEW)
        cache.set_host("HOSTB")
        self.assertFalse(cache.put_if_current(query_a, _overview("HOSTA")))
        self.assertIsNone(cache.get(InventorySection.OVERVIEW))
        self.assertEqual(cache.host, "hostb")

    def test_stale_request_same_host_is_rejected(self) -> None:
        cache = InventoryCache()
        first = cache.begin_query("HOSTA", InventorySection.OVERVIEW)
        second = cache.begin_query("HOSTA", InventorySection.OVERVIEW)
        self.assertTrue(cache.put_if_current(second, _overview("NEW")))
        self.assertFalse(cache.put_if_current(first, _overview("OLD")))
        cached = cache.get(InventorySection.OVERVIEW)
        self.assertIsNotNone(cached)
        self.assertEqual(cached.hostname, "NEW")

    def test_invalidate_then_old_put_fails(self) -> None:
        cache = InventoryCache()
        query = cache.begin_query("HOSTA", InventorySection.OVERVIEW)
        wrote = threading.Event()
        invalidated = threading.Event()
        accepted: list[bool] = []

        def invalidator() -> None:
            cache.invalidate()
            invalidated.set()

        def writer() -> None:
            self.assertTrue(invalidated.wait(timeout=5))
            accepted.append(cache.put_if_current(query, _overview("HOSTA")))
            wrote.set()

        t_inv = threading.Thread(target=invalidator)
        t_put = threading.Thread(target=writer)
        t_inv.start()
        t_put.start()
        t_inv.join(timeout=5)
        t_put.join(timeout=5)
        self.assertTrue(wrote.is_set())
        self.assertEqual(accepted, [False])
        self.assertIsNone(cache.get(InventorySection.OVERVIEW))

    def test_put_then_invalidate_leaves_cache_empty(self) -> None:
        cache = InventoryCache()
        query = cache.begin_query("HOSTA", InventorySection.OVERVIEW)
        written = threading.Event()

        def writer() -> None:
            self.assertTrue(cache.put_if_current(query, _overview("HOSTA")))
            written.set()

        def invalidator() -> None:
            self.assertTrue(written.wait(timeout=5))
            cache.invalidate()

        t_put = threading.Thread(target=writer)
        t_inv = threading.Thread(target=invalidator)
        t_put.start()
        t_inv.start()
        t_put.join(timeout=5)
        t_inv.join(timeout=5)
        self.assertIsNone(cache.get(InventorySection.OVERVIEW))
        self.assertFalse(cache.put_if_current(query, _overview("HOSTA")))

    def test_concurrent_invalidate_and_put_never_keep_stale_data(self) -> None:
        cache = InventoryCache()
        query = cache.begin_query("HOSTA", InventorySection.OVERVIEW)
        start = threading.Barrier(2)

        def writer() -> None:
            start.wait(timeout=5)
            cache.put_if_current(query, _overview("HOSTA"))

        def invalidator() -> None:
            start.wait(timeout=5)
            cache.invalidate()

        t_put = threading.Thread(target=writer)
        t_inv = threading.Thread(target=invalidator)
        t_put.start()
        t_inv.start()
        t_put.join(timeout=5)
        t_inv.join(timeout=5)
        self.assertIsNone(cache.get(InventorySection.OVERVIEW))
        self.assertFalse(cache.is_current(query))
        self.assertFalse(cache.put_if_current(query, _overview("LATE")))
        self.assertIsNone(cache.get(InventorySection.OVERVIEW))

    def test_get_if_current_rejects_other_generation(self) -> None:
        cache = InventoryCache()
        query = cache.begin_query("HOSTA", InventorySection.OVERVIEW)
        cache.put_if_current(query, _overview("HOSTA"))
        cache.invalidate()
        self.assertIsNone(cache.get_if_current(query))


class InventoryServiceIdentityTests(unittest.TestCase):
    def test_old_result_after_host_change_is_not_cached(self) -> None:
        gate = _CollectorGate()
        gate.add(_overview("HOSTA"))
        service = InventoryService(collectors={InventorySection.OVERVIEW: gate.collect})
        query = service.begin_query("HOSTA", InventorySection.OVERVIEW)
        result_holder: list[SectionResult] = []

        def run_collect() -> None:
            result_holder.append(
                service.collect_section(
                    InventorySection.OVERVIEW,
                    "HOSTA",
                    query=query,
                )
            )

        worker = threading.Thread(target=run_collect)
        worker.start()
        self.assertTrue(gate.started.wait(timeout=5))
        service.bind_host("HOSTB")
        service.invalidate_all()
        gate.release.set()
        worker.join(timeout=5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(service.cache.host, "hostb")
        self.assertIsNone(service.cache.get(InventorySection.OVERVIEW))
        self.assertEqual(result_holder[0].payload, None)
        self.assertEqual(result_holder[0].query, query)

    def test_a_b_a_old_response_does_not_replace_current(self) -> None:
        first_gate = _CollectorGate()
        first_gate.add(_overview("OLD-A"))
        second_gate = _CollectorGate()
        second_gate.add(_overview("NEW-A"))
        calls = {"n": 0}

        def collect(host: str, **kwargs: object) -> object:
            calls["n"] += 1
            if calls["n"] == 1:
                return first_gate.collect(host, **kwargs)
            return second_gate.collect(host, **kwargs)

        service = InventoryService(collectors={InventorySection.OVERVIEW: collect})
        first = service.begin_query("HOSTA", InventorySection.OVERVIEW)
        first_result: list[SectionResult] = []
        t1 = threading.Thread(
            target=lambda: first_result.append(
                service.collect_section(InventorySection.OVERVIEW, "HOSTA", query=first)
            )
        )
        t1.start()
        self.assertTrue(first_gate.started.wait(timeout=5))

        service.bind_host("HOSTB")
        service.invalidate_all()
        second = service.begin_query("HOSTA", InventorySection.OVERVIEW)
        second_gate.release.set()
        current = service.collect_section(InventorySection.OVERVIEW, "HOSTA", query=second)
        self.assertEqual(current.payload.hostname, "NEW-A")

        first_gate.release.set()
        t1.join(timeout=5)
        cached = service.cache.get(InventorySection.OVERVIEW)
        self.assertIsNotNone(cached)
        self.assertEqual(cached.hostname, "NEW-A")
        self.assertIsNone(first_result[0].payload)

    def test_older_refresh_does_not_overwrite_newer(self) -> None:
        first_gate = _CollectorGate()
        first_gate.add(_overview("FIRST"))
        second_gate = _CollectorGate()
        second_gate.add(_overview("SECOND"))
        calls = {"n": 0}

        def collect(host: str, **kwargs: object) -> object:
            calls["n"] += 1
            if calls["n"] == 1:
                return first_gate.collect(host, **kwargs)
            return second_gate.collect(host, **kwargs)

        service = InventoryService(collectors={InventorySection.OVERVIEW: collect})
        first = service.begin_query("HOSTA", InventorySection.OVERVIEW)
        first_result: list[SectionResult] = []
        t1 = threading.Thread(
            target=lambda: first_result.append(
                service.collect_section(InventorySection.OVERVIEW, "HOSTA", query=first)
            )
        )
        t1.start()
        self.assertTrue(first_gate.started.wait(timeout=5))

        service.invalidate_section(InventorySection.OVERVIEW)
        second = service.begin_query("HOSTA", InventorySection.OVERVIEW)
        second_gate.release.set()
        current = service.collect_section(
            InventorySection.OVERVIEW, "HOSTA", force=True, query=second
        )
        self.assertEqual(current.payload.hostname, "SECOND")

        first_gate.release.set()
        t1.join(timeout=5)
        self.assertEqual(service.cache.get(InventorySection.OVERVIEW).hostname, "SECOND")
        self.assertIsNone(first_result[0].payload)

    def test_late_error_does_not_write_cache(self) -> None:
        def boom(host: str, **_kwargs: object) -> HardwareData:
            raise RuntimeError(f"falha remota de {host}")

        service = InventoryService(collectors={InventorySection.HARDWARE: boom})
        stale = service.begin_query("HOSTA", InventorySection.HARDWARE)
        service.invalidate_all()
        current = service.begin_query("HOSTB", InventorySection.HARDWARE)
        service.cache.put_if_current(current, _hardware("HOSTB"))

        result = service.collect_section(
            InventorySection.HARDWARE, "HOSTA", query=stale
        )
        self.assertEqual(result.error, "Consulta obsoleta.")
        cached = service.cache.get(InventorySection.HARDWARE)
        self.assertIsNotNone(cached)
        self.assertEqual(cached.error, "hw-HOSTB")

    def test_successful_collect_and_cache_reuse(self) -> None:
        calls = {"n": 0}

        def collect(host: str, **_kwargs: object) -> OverviewData:
            calls["n"] += 1
            return _overview(host)

        service = InventoryService(collectors={InventorySection.OVERVIEW: collect})
        first = service.collect_section(InventorySection.OVERVIEW, "HOSTA")
        second = service.collect_section(InventorySection.OVERVIEW, "HOSTA")
        self.assertEqual(first.payload.hostname, "HOSTA")
        self.assertIs(second.payload, first.payload)
        self.assertEqual(calls["n"], 1)

    def test_query_context_identity_is_not_hostname_only(self) -> None:
        a1 = QueryContext("hosta", InventorySection.OVERVIEW, 1, 1)
        a2 = QueryContext("hosta", InventorySection.OVERVIEW, 2, 1)
        self.assertFalse(a1.matches(a2))
        self.assertTrue(a1.matches(QueryContext("hosta", InventorySection.OVERVIEW, 1, 1)))


if __name__ == "__main__":
    unittest.main()
